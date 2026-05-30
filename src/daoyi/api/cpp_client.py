"""C++ LLM Engine Python 适配器。

实现 SupportsStreamingMessages 协议，在 _daoyi 可用时
将 LLM 调用委派给 C++ LLMEngine，否则回退到 Python 客户端。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator

from daoyi.api.client import (
    ApiMessageCompleteEvent,
    ApiRetryEvent,
    ApiTextDeltaEvent,
)

log = logging.getLogger(__name__)

try:
    import _daoyi as _CPP
    _HAS_CPP = True
except ImportError:
    _CPP = None
    _HAS_CPP = False


class CppLLMClient:
    """C++-backed LLM client.

    当 _daoyi 扩展可用时，直接调用 C++ LLMEngine。
    否则通过 fallback_client 委派。
    """

    def __init__(
        self,
        fallback_client=None,
        host: str = "localhost",
        port: int = 8080,
        api_key: str = "",
        use_local: bool = False,
        model_path: str = "",
    ) -> None:
        self._fallback = fallback_client
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._warmup_done = False

        if _HAS_CPP and use_local:
            self._engine = _CPP.create_llm_engine()
            if model_path:
                cfg = _CPP.GPUConfig()
                cfg.mode = "local"
                self._engine.load_model_with_config(model_path, cfg)
                # Trigger GPU warmup in background thread
                self._warmup_thread = threading.Thread(
                    target=self._warmup_engine, daemon=True,
                )
                self._warmup_thread.start()
            self._mode = "cpp_local"
            log.info("CppLLMClient: using C++ local engine")
        elif _HAS_CPP and host and host != "localhost":
            self._engine = _CPP.create_remote_llm(host, port, api_key, "/v1/chat/completions")
            self._mode = "cpp_remote"
            log.info("CppLLMClient: using C++ remote engine (host=%s:%d)", host, port)
        else:
            self._engine = None
            self._mode = "fallback"
            log.info("CppLLMClient: using fallback Python client")

    def _warmup_engine(self) -> None:
        """Run Metal GPU warmup in a background thread."""
        try:
            log.info("CppLLMClient: starting GPU warmup (Metal kernel compilation)...")
            ok = self._engine.warmup()
            self._warmup_done = True
            if ok:
                log.info("CppLLMClient: GPU warmup complete")
            else:
                log.warning("CppLLMClient: GPU warmup returned failure")
        except Exception as e:
            log.warning("CppLLMClient: GPU warmup failed: %s", e)
            self._warmup_done = True

    def __del__(self) -> None:
        """Clean up C++ engine resources."""
        if self._engine is not None and _HAS_CPP:
            try:
                self._engine.unload_model()
            except Exception:
                pass

    # ── message conversion ─────────────────────────────────────────

    @staticmethod
    def _msg_to_dict(msg) -> dict:
        """Convert any message-like object to C++-compatible dict."""
        if isinstance(msg, dict):
            role = msg.get("role", "user")
            raw_content = msg.get("content", "")
        else:
            role = getattr(msg, "role", "user")
            raw_content = getattr(msg, "content", "")
        text_parts = []
        if isinstance(raw_content, list):
            for block in raw_content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
                elif hasattr(block, "content") and hasattr(block, "tool_use_id"):
                    text_parts.append(f"<tool_result>{block.content}</tool_result>")
                elif hasattr(block, "name"):
                    text_parts.append(f"<tool_use>{block.name}</tool_use>")
                else:
                    text_parts.append(str(block))
        else:
            text_parts.append(str(raw_content))
        return {"role": role, "content": "\n".join(text_parts)}

    @staticmethod
    def _tool_schema_to_cpp(tools: list[dict]) -> list[dict[str, str]]:
        """Flatten tool schema dicts for C++ (string-string maps)."""
        return [
            {"name": t.get("name", ""), "description": t.get("description", "")}
            for t in (tools or [])
        ]

    # ── stream_message ─────────────────────────────────────────────

    async def stream_message(self, request) -> AsyncIterator:
        """Stream an LLM response.

        Delegate to C++ engine when available, otherwise to fallback.
        """
        if self._mode != "fallback" and self._engine is not None:
            async for event in self._stream_via_cpp(request):
                yield event
        elif self._fallback is not None:
            async for event in self._fallback.stream_message(request):
                yield event
        else:
            yield self._empty_completion("error")

    async def _stream_via_cpp(self, request) -> AsyncIterator:
        """Stream via C++ LLMEngine (blocking infer in executor)."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async for event in self._stream_once_cpp(request):
                    yield event
                return
            except Exception as exc:
                log.warning("C++ LLM attempt %d failed: %s", attempt + 1, exc)
                if attempt < max_retries - 1:
                    yield ApiRetryEvent(
                        message=str(exc), attempt=attempt + 1,
                        max_attempts=max_retries, delay_seconds=1.0,
                    )
                    await asyncio.sleep(1.0)
                else:
                    yield self._empty_completion("error")

    async def _stream_once_cpp(self, request) -> AsyncIterator:
        """Single C++ LLM inference call."""
        messages = [self._msg_to_dict(m) for m in request.messages]
        tools = self._tool_schema_to_cpp(request.tools) if request.tools else []

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            self._executor, self._engine.infer_full,
            messages, tools,
            request.max_tokens, 0.7,
        )

        text = result.get("text", "")
        tool_calls = list(result.get("tool_calls", []))
        tokens_used = result.get("tokens_used", 0)

        if text:
            yield ApiTextDeltaEvent(text=text)

        yield self._build_completion(text, tool_calls, tokens_used)

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _build_completion(text: str, tool_calls: list[dict], tokens: int):
        """Build ApiMessageCompleteEvent from C++ result parts."""
        from daoyi.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
        from daoyi.api.usage import UsageSnapshot

        content: list = []
        if text:
            content.append(TextBlock(text=text))
        for tc in tool_calls:
            content.append(ToolUseBlock(
                id=tc.get("id", ""),
                name=tc.get("name", ""),
                input=tc.get("arguments", {}),
            ))
        return ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=content),
            usage=UsageSnapshot(input_tokens=0, output_tokens=int(tokens)),
            stop_reason="end_turn" if not tool_calls else "tool_use",
        )

    @staticmethod
    def _empty_completion(stop_reason: str = "error"):
        """Return empty completion (model not loaded or error)."""
        from daoyi.engine.messages import ConversationMessage
        from daoyi.api.usage import UsageSnapshot

        return ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=[]),
            usage=UsageSnapshot(),
            stop_reason=stop_reason,
        )
