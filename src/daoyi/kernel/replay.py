"""ReplayEngine — 确定性回放与 LLM 响应缓存。

基于请求 hash 缓存 LLM 输出，相同输入直接回放，零推理成本。
同时录制会话 trace 到磁盘供回放/调试/回归测试。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

from daoyi.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiRetryEvent,
    ApiTextDeltaEvent,
    SupportsStreamingMessages,
)
from daoyi.api.usage import UsageSnapshot

log = logging.getLogger(__name__)

_MAX_CACHE_ENTRIES = 5000  # rough upper bound for replay cache on disk
_MAX_TRACE_FILES = 50      # max trace files before cleanup


# ── Cache directory ───────────────────────────────────────────────

def _cache_dir() -> Path:
    base = Path(os.environ.get("DAOYI_HOME", "~/.daoyi")).expanduser()
    path = base / "cache" / "replay"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _trace_dir() -> Path:
    base = Path(os.environ.get("DAOYI_HOME", "~/.daoyi")).expanduser()
    path = base / "traces"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── Request hashing ───────────────────────────────────────────────


def _hash_request(req: ApiMessageRequest) -> str:
    """Deterministic hash of an API request."""
    raw = json.dumps(
        {
            "model": req.model,
            "messages": [
                {
                    "role": m.role,
                    "content": _serialize_content(m.content),
                }
                for m in req.messages
            ],
            "system": req.system_prompt,
            "max_tokens": req.max_tokens,
            "tools": req.tools,
            "effort": req.effort,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _serialize_content(content: Any) -> Any:
    """Serialize message content deterministically."""
    if isinstance(content, list):
        return [_serialize_content(c) for c in content]
    if hasattr(content, "text"):
        return {"type": "text", "text": content.text}
    if hasattr(content, "content"):
        return {"type": "tool_result", "content": content.content}
    if hasattr(content, "name"):
        # ToolUseBlock-like
        return {
            "type": "tool_use",
            "name": content.name,
            "input": content.input if hasattr(content, "input") else {},
        }
    if hasattr(content, "model_dump"):
        return content.model_dump()
    return str(content)


# ── ReplayEngine ──────────────────────────────────────────────────


class ReplayEngine(SupportsStreamingMessages):
    """LLM 回放引擎 — 带缓存和 trace 录制。

    用法:
      engine = ReplayEngine(wrapped_client, enable_cache=True, record_trace=True)
      async for event in engine.stream_message(request):
          ...
    """

    def __init__(
        self,
        inner: SupportsStreamingMessages,
        enable_cache: bool = True,
        record_trace: bool = True,
        session_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._enable_cache = enable_cache
        self._record_trace = record_trace
        self._session_id = session_id or f"session-{int(time.time())}"
        self._cache_hits = 0
        self._cache_misses = 0
        self._trace: list[dict] = []

    @property
    def cache_hit_rate(self) -> float:
        total = self._cache_hits + self._cache_misses
        return self._cache_hits / total if total else 0.0

    @property
    def trace(self) -> list[dict]:
        return list(self._trace)

    # ── 流式接口 ──

    async def stream_message(
        self, request: ApiMessageRequest
    ) -> AsyncIterator[ApiTextDeltaEvent | ApiMessageCompleteEvent | ApiRetryEvent]:
        """流式调用 LLM，带缓存 + trace 录制。

        与原始 client.stream_message 接口完全兼容。
        """
        req_hash = _hash_request(request)

        # 尝试缓存命中
        if self._enable_cache:
            cached = self._load_cached(req_hash)
            if cached is not None:
                self._cache_hits += 1
                for event_data in cached:
                    if event_data["type"] == "delta":
                        yield ApiTextDeltaEvent(text=event_data["text"])
                    elif event_data["type"] == "complete":
                        msg = _deserialize_message(event_data["message"])
                        yield ApiMessageCompleteEvent(
                            message=msg,
                            usage=UsageSnapshot(),
                        )
                return

        self._cache_misses += 1

        # 调用底层 LLM
        events: list[dict] = []
        final_message = None
        t0 = time.monotonic()

        async for event in self._inner.stream_message(request):
            if isinstance(event, ApiTextDeltaEvent):
                events.append({"type": "delta", "text": event.text})
                yield event
            elif isinstance(event, ApiMessageCompleteEvent):
                final_message = event.message
                events.append({
                    "type": "complete",
                    "message": _serialize_message(event.message),
                })
                yield event
            elif hasattr(event, '__class__') and event.__class__.__name__ == 'ApiRetryEvent':
                # Pass through retry events without caching
                yield event

        duration = time.monotonic() - t0

        # 缓存 (skip empty/failed responses to avoid cache poisoning)
        if self._enable_cache and final_message is not None and not final_message.is_effectively_empty():
            self._save_cached(req_hash, events)

        # Trace 录制
        if self._record_trace:
            self._trace.append({
                "hash": req_hash,
                "model": request.model,
                "messages": _serialize_content(request.messages),
                "system_prompt": request.system_prompt,
                "max_tokens": request.max_tokens,
                "tools": request.tools,
                "events": events,
                "duration_s": round(duration, 3),
                "timestamp": time.time(),
            })

    # ── Trace 持久化 ──

    def flush_trace(self) -> str:
        """将当前 trace 写入磁盘。返回 trace 文件路径。"""
        if not self._trace:
            return ""
        trace_path = _trace_dir() / f"{self._session_id}.jsonl"
        with open(trace_path, "a") as f:
            for entry in self._trace:
                f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
        n = len(self._trace)
        self._trace.clear()
        log.info("flushed %d trace entries to %s", n, trace_path)
        self._evict_old_traces()
        return str(trace_path)

    @staticmethod
    def _evict_old_traces() -> None:
        """Keep trace directory bounded."""
        trace_dir = _trace_dir()
        try:
            files = sorted(trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            if len(files) > _MAX_TRACE_FILES:
                for stale in files[: len(files) - _MAX_TRACE_FILES]:
                    stale.unlink(missing_ok=True)
                log.info("evicted %d stale trace files", len(files) - _MAX_TRACE_FILES)
        except OSError:
            pass

    # ── 缓存 I/O ──

    def _load_cached(self, req_hash: str) -> list[dict] | None:
        """从磁盘加载缓存的事件序列。"""
        cache_path = _cache_dir() / f"{req_hash}.json"
        if cache_path.exists():
            try:
                with open(cache_path) as f:
                    data = json.load(f)
                log.debug("replay cache HIT  %s", req_hash)
                return data
            except (json.JSONDecodeError, OSError) as e:
                log.warning("replay cache read error %s: %s", req_hash, e)
        return None

    def _save_cached(self, req_hash: str, events: list[dict]) -> None:
        """将事件序列写入磁盘缓存。"""
        cache_path = _cache_dir() / f"{req_hash}.json"
        try:
            with open(cache_path, "w") as f:
                json.dump(events, f, default=str, ensure_ascii=False)
            log.debug("replay cache SAVE %s (%d events)", req_hash, len(events))
        except OSError as e:
            log.warning("replay cache write error %s: %s", req_hash, e)
        self._evict_cache_if_needed()

    def _evict_cache_if_needed(self) -> None:
        """Keep replay cache disk usage bounded."""
        cache_dir = _cache_dir()
        try:
            files = sorted(cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            if len(files) > _MAX_CACHE_ENTRIES:
                for stale in files[: len(files) - _MAX_CACHE_ENTRIES]:
                    stale.unlink(missing_ok=True)
                log.info("evicted %d stale replay cache entries", len(files) - _MAX_CACHE_ENTRIES)
        except OSError:
            pass

    # ── 回放模式 ──

    @classmethod
    async def replay_trace(
        cls, trace_path: str, request_filter: dict | None = None
    ) -> AsyncIterator[ApiTextDeltaEvent | ApiMessageCompleteEvent]:
        """从 trace 文件回放，不调 LLM。

        Args:
            trace_path: .jsonl trace 文件路径
            request_filter: 过滤条件（如 {"model": "..."}）
        """
        path = Path(trace_path)
        if not path.exists():
            log.error("trace file not found: %s", trace_path)
            return

        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if request_filter:
                    if not all(entry.get(k) == v for k, v in request_filter.items()):
                        continue
                for ev in entry.get("events", []):
                    if ev["type"] == "delta":
                        yield ApiTextDeltaEvent(text=ev["text"])
                    elif ev["type"] == "complete":
                        yield _deserialize_message(ev["message"])


# ── 序列化助手 ───────────────────────────────────────────────────


def _serialize_message(msg) -> dict:
    """将 ConversationMessage 序列化为可 JSON 的 dict。"""
    content_list = []
    for block in (msg.content if isinstance(msg.content, list) else [msg.content]):
        content_list.append(_serialize_content(block))
    return {
        "role": msg.role,
        "content": content_list,
    }


def _deserialize_message(data: dict):
    """从 dict 重建 ConversationMessage。"""
    from daoyi.engine.messages import (
        ConversationMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    content: list = []
    for item in data.get("content", []):
        t = item.get("type", "")
        if t == "text":
            content.append(TextBlock(text=item.get("text", "")))
        elif t == "tool_use":
            from daoyi.engine.messages import ToolUseBlock
            content.append(ToolUseBlock(
                id=item.get("id", ""),
                name=item.get("name", ""),
                input=item.get("input", {}),
            ))
        elif t == "tool_result":
            content.append(ToolResultBlock(
                tool_use_id=item.get("tool_use_id", ""),
                content=item.get("content", ""),
                is_error=item.get("is_error", False),
            ))
    return ConversationMessage(role=data.get("role", "assistant"), content=content)
