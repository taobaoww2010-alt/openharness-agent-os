"""MemoryManager — 三层缓存架构的统一上下文管理。

类比：
  L1 = 寄存器 (context window 中当前 messages[])
  L2 = CPU Cache (工具结果缓存、phase 摘要)
  L3 = 主存 (已知文件列表、持久化工作区)

所有 workflow 执行中的状态都通过 MemoryManager 统一管理，
不再分散在 executor 的多个局部变量中。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── C++ 核心桥接 ──

try:
    import _daoyi as _CPP
    _HAS_CPP = True
except ImportError:
    _CPP = None
    _HAS_CPP = False


# ── C++ 消息转换 ──

def _msg_role_to_int(msg) -> int:
    role = getattr(msg, "role", "user")
    if isinstance(role, str):
        return {"system": 0, "user": 1, "assistant": 2, "tool": 3}.get(role, 1)
    return role if isinstance(role, int) else 1

def _msg_content_str(msg) -> str:
    content = getattr(msg, "content", str(msg))
    if isinstance(content, list):
        parts = []
        for b in content:
            if hasattr(b, "text") and b.text:
                parts.append(b.text)
            elif hasattr(b, "content"):
                parts.append(str(b.content)[:500])
            else:
                parts.append(str(b)[:200])
        return "\n".join(parts)
    return str(content)[:2000]

# ── L3 持久化路径 ────────────────────────────────────────────────

_L3_DIR = Path(
    os.environ.get("DAOYI_L3_DIR")
    or os.environ.get("OPENHARNESS_L3_DIR")
    or Path.home() / ".daoyi" / "l3"
)

# ── Session memory ────────────────────────────────────────────────
# Max session memory files to keep (oldest evicted first)
_SESSION_MEMORY_MAX = int(os.environ.get("DAOYI_SESSION_MEMORY_MAX") or os.environ.get("OPENHARNESS_SESSION_MEMORY_MAX") or "20")
# TTL for session memory in seconds (default 7 days)
_SESSION_MEMORY_TTL = int(os.environ.get("DAOYI_SESSION_MEMORY_TTL") or os.environ.get("OPENHARNESS_SESSION_MEMORY_TTL") or str(7 * 86400))

# ── 配置 ──────────────────────────────────────────────────────────

# L1: context 超限阈值（占总限百分比）
CONTEXT_EVICT_THRESHOLD = 0.70
# L1: 摘要后保留的最新消息数
CONTEXT_KEEP_RECENT = 4

# L2: 单条工具结果缓存上限（字符）
TOOL_CACHE_LIMIT = 4000

# L3: 阶段摘要长度上限
PHASE_SUMMARY_LIMIT = 500


# ── MemoryManager ─────────────────────────────────────────────────

class MemoryManager:
    """三层缓存管理器。一个 WorkflowExecutor 持有一个实例。"""

    def __init__(self, context_window_limit: int = 65536) -> None:
        self._context_limit = context_window_limit

        # ── C++ 后端 ──
        self._cpp = _CPP.create_memory_manager(context_window_limit) if _HAS_CPP else None

        # ── L1: Context Window ──
        #   当前 phase 的 messages[]（包括 assistant + tool_results）
        self._l1_messages: list = []  # ConversationMessage list
        self._l1_turn_count = 0

        # ── L2: Session Cache ──
        #   工具结果缓存 (key=工具名+参数hash → 输出文本)
        self._l2_tool_cache: dict[str, str] = {}
        #   阶段摘要 (phase name → 摘要文本)
        self._l2_phase_summaries: list[str] = []
        #   不缓存的可变工具
        self._l2_no_cache = {"write_file", "bash", "read_file", "edit_file"}

        # ── L3: Workspace ──
        #   已知文件列表 (write_file 创建的文件路径)
        self._l3_known_files: list[str] = []
        #   累计上下文（跨 phase 传递的文本摘要）
        self._l3_accumulated_context: str = ""
        #   已用的工具名集合
        self._l3_tools_used: list[str] = []

        # ── Metrics ──
        self._l2_hits = 0
        self._l2_misses = 0
        self._evict_count = 0

    # ── L3: Workspace ────────────────────────────────────────────

    @property
    def known_files(self) -> list[str]:
        return self._l3_known_files

    def add_known_file(self, path: str) -> None:
        if path and path not in self._l3_known_files:
            self._l3_known_files.append(path)

    @property
    def accumulated_context(self) -> str:
        if self._cpp:
            val = self._cpp.l3_get()
            # C++ l3_append appends "\n" — strip trailing newlines for Python
            return val.rstrip('\n')
        return self._l3_accumulated_context

    @accumulated_context.setter
    def accumulated_context(self, value: str) -> None:
        self._l3_accumulated_context = value
        if self._cpp:
            self._cpp.l3_clear()
            self._cpp.l3_append(value)

    @property
    def tools_used(self) -> list[str]:
        return self._l3_tools_used

    def record_tool_use(self, name: str) -> None:
        self._l3_tools_used.append(name)

    # ── L3 持久化 ────────────────────────────────────────────────

    def _l3_path(self, workflow_id: str) -> Path:
        return _L3_DIR / f"{workflow_id}.json"

    def save_checkpoint(self, workflow_id: str) -> None:
        """Persist L3 context + known_files + phase summaries to disk."""
        path = self._l3_path(workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx = self.accumulated_context  # reads from C++ if available
        data = {
            "accumulated_context": ctx,
            "known_files": self._l3_known_files,
            "phase_summaries": self._l2_phase_summaries,
            "tools_used": list(set(self._l3_tools_used)),
            "updated_at": time.time(),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def load_checkpoint(self, workflow_id: str) -> bool:
        """Restore L3 context from disk. Returns True if data was loaded."""
        path = self._l3_path(workflow_id)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
            self._l3_accumulated_context = data.get("accumulated_context", "")
            self._l3_known_files = data.get("known_files", [])
            self._l2_phase_summaries = data.get("phase_summaries", [])
            # Sync to C++ MemoryManager
            if self._cpp:
                self._cpp.l3_clear()
                if self._l3_accumulated_context:
                    self._cpp.l3_append(self._l3_accumulated_context)
                # Read back from C++ as primary
                self._l3_accumulated_context = self._cpp.l3_get().rstrip('\n')
            return True
        except (json.JSONDecodeError, OSError) as e:
            log.warning("failed to load L3 checkpoint: %s", e)
            return False

    # ── Session Memory (cross-session persistence) ──────────────

    def save_session_memory(
        self, session_id: str, user_intent: str, outcome_summary: str,
    ) -> None:
        """Save a compact session memory entry for future sessions.

        Stores: key decisions, files created, tools used, and the outcome.
        Old entries are pruned when count exceeds _SESSION_MEMORY_MAX.
        """
        path = _L3_DIR / "sessions" / f"{session_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "session_id": session_id,
            "user_intent": user_intent[:500],
            "outcome_summary": outcome_summary[:1000],
            "known_files": self._l3_known_files[:20],
            "tools_used": list(set(self._l3_tools_used)),
            "created_at": time.time(),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        self._prune_session_memories()

    def load_session_memories(self, max_entries: int = 5) -> str:
        """Return recent session memories formatted for prompt injection.

        Excludes entries older than _SESSION_MEMORY_TTL.
        """
        mem_dir = _L3_DIR / "sessions"
        if not mem_dir.is_dir():
            return ""
        now = time.time()
        entries: list[dict] = []
        for f in sorted(mem_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.suffix != ".json":
                continue
            try:
                data = json.loads(f.read_text())
                age = now - data.get("created_at", 0)
                if age > _SESSION_MEMORY_TTL:
                    f.unlink(missing_ok=True)
                    continue
                entries.append(data)
                if len(entries) >= max_entries:
                    break
            except (json.JSONDecodeError, OSError):
                continue
        if not entries:
            return ""
        blocks = []
        for e in reversed(entries):
            intent = e.get("user_intent", "")[:200]
            outcome = e.get("outcome_summary", "")[:300]
            files = e.get("known_files", [])
            tools = e.get("tools_used", [])
            block = f"[Session: {e['session_id'][:8]}]\n  Intent: {intent}"
            if outcome:
                block += f"\n  Outcome: {outcome}"
            if files:
                block += f"\n  Files: {', '.join(files[:5])}"
            if tools:
                block += f"\n  Tools: {', '.join(tools[:5])}"
            blocks.append(block)
        return "\n\n".join(blocks)

    @staticmethod
    def _prune_session_memories() -> None:
        """Remove oldest session memory files beyond _SESSION_MEMORY_MAX."""
        mem_dir = _L3_DIR / "sessions"
        if not mem_dir.is_dir():
            return
        files = sorted(
            [f for f in mem_dir.iterdir() if f.suffix == ".json"],
            key=lambda p: p.stat().st_mtime,
        )
        while len(files) > _SESSION_MEMORY_MAX:
            oldest = files.pop(0)
            try:
                oldest.unlink(missing_ok=True)
            except OSError:
                pass

    # ── L2: Tool Cache ───────────────────────────────────────────

    @staticmethod
    def _tool_cache_key(name: str, inp: dict) -> str:
        return f"{name}:{json.dumps(inp, sort_keys=True, default=str)}"

    def get_cached_tool_result(self, name: str, inp: dict, tool_use_id: str):
        """Return cached ToolResultBlock or None."""
        if name in self._l2_no_cache:
            self._l2_misses += 1
            return None
        key = self._tool_cache_key(name, inp)
        if self._cpp:
            cached = self._cpp.l2_get(key)
        else:
            cached = self._l2_tool_cache.get(key)
        if cached is not None:
            self._l2_hits += 1
            from daoyi.engine.messages import ToolResultBlock
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=cached,
                is_error=False,
            )
        self._l2_misses += 1
        return None

    def set_cached_tool_result(self, name: str, inp: dict, output: str) -> None:
        if name in self._l2_no_cache:
            return
        key = self._tool_cache_key(name, inp)
        if self._cpp:
            self._cpp.l2_set(key, output)
        self._l2_tool_cache[key] = output

    @property
    def l2_hit_rate(self) -> float:
        if self._cpp:
            return self._cpp.l2_hit_rate()
        total = self._l2_hits + self._l2_misses
        return self._l2_hits / total if total else 0.0

    # ── L1: Context Window ───────────────────────────────────────

    @property
    def l1_messages(self) -> list:
        return self._l1_messages

    def l1_set_initial(self, messages: list) -> None:
        """Set initial messages for a new phase (replaces L1)."""
        self._l1_messages = messages
        self._l1_turn_count = 0
        if self._cpp and hasattr(self._cpp, 'l1_set_initial'):
            cpp_msgs = [
                _CPP.Message(_msg_role_to_int(m), _msg_content_str(m))
                for m in messages
            ]
            self._cpp.l1_set_initial(cpp_msgs)
        elif self._cpp:
            self._cpp.l1_clear()
            for m in messages:
                self._cpp.l1_append(
                    _msg_role_to_int(m),
                    _msg_content_str(m),
                )

    def l1_append_turn(self, assistant_msg, tool_results: list) -> None:
        """Append assistant + tool results after a turn."""
        self._l1_messages.append(assistant_msg)
        from daoyi.engine.messages import ConversationMessage
        self._l1_messages.append(
            ConversationMessage(role="user", content=tool_results)
        )
        self._l1_turn_count += 1
        if self._cpp:
            self._cpp.l1_append(2, _msg_content_str(assistant_msg))
            self._cpp.l1_append(1, str(tool_results)[:2000])
        self._maybe_evict()

    def l1_context_size(self) -> int:
        """Estimate char-length of current L1 messages (proxy for token count)."""
        return sum(
            len(str(getattr(m, "content", m)))
            for m in self._l1_messages
        )

    def _maybe_evict(self) -> None:
        """When L1 exceeds threshold, evict oldest tool results to summaries."""
        size = self.l1_context_size()
        if size < self._context_limit * CONTEXT_EVICT_THRESHOLD:
            return

        # Keep recent messages, summarize older tool-result pairs
        if len(self._l1_messages) <= CONTEXT_KEEP_RECENT * 2:
            return

        # Count pairs (assistant + user tool-result) to remove
        removable = self._l1_messages[: -CONTEXT_KEEP_RECENT * 2]
        kept = self._l1_messages[-CONTEXT_KEEP_RECENT * 2:]

        # Summarize what was removed
        summary_parts: list[str] = []
        for m in removable:
            if hasattr(m, "text") and m.text:
                summary_parts.append(m.text[:200])
            elif hasattr(m, "content") and isinstance(m.content, list):
                for block in m.content:
                    if hasattr(block, "content") and isinstance(block.content, str):
                        summary_parts.append(block.content[:200])

        if summary_parts:
            from daoyi.engine.messages import ConversationMessage, TextBlock
            summary_text = "[早期对话摘要]\n" + "\n".join(summary_parts)
            # Prepend summary to kept messages
            self._l1_messages = [
                ConversationMessage(
                    role="user",
                    content=[TextBlock(text=summary_text)],
                )
            ] + kept

        self._evict_count += 1

    # ── Phase Transitions ────────────────────────────────────────

    def squash_for_next_phase(
        self, phase_name: str, phase_results: list[str],
        workflow_id: str = "",
    ) -> str:
        """Compress phase state into a summary string for next phase.

        Returns the summary text.
        """
        last_result = phase_results[-1][:PHASE_SUMMARY_LIMIT] if phase_results else ""

        summary = f"\n[Phase {phase_name} complete]"
        if last_result:
            summary += "\n" + last_result
        if self._l3_known_files:
            summary += "\nFiles: " + ", ".join(self._l3_known_files)

        self._l2_phase_summaries.append(summary)
        if self._cpp:
            self._cpp.l3_append(summary)
            self._l3_accumulated_context = self._cpp.l3_get().rstrip('\n')
        else:
            self._l3_accumulated_context += summary

        # Reset L1 for next phase
        self._l1_messages = []
        self._l1_turn_count = 0
        if self._cpp:
            self._cpp.l1_clear()

        # Persist L3 to disk for session recovery
        if workflow_id:
            self.save_checkpoint(workflow_id)

        return summary

    def make_files_context(self) -> str:
        """Build '已创建的文件' context block from L3."""
        if not self._l3_known_files:
            return ""
        return (
            "已创建的文件：\n"
            + "\n".join(f"  - {f}" for f in self._l3_known_files)
            + "\n\n"
        )

    def format_phase_results_summary(self, phase_results: list[str]) -> str:
        """Format phase results for prompt injection."""
        return "\n".join(
            f"  [{i+1}] {r}" for i, r in enumerate(phase_results)
        )

    # ── Utilities ─────────────────────────────────────────────────

    @staticmethod
    def truncate(content: str) -> str:
        if len(content) > TOOL_CACHE_LIMIT:
            return content[:TOOL_CACHE_LIMIT] + "\n... [output truncated]"
        return content

    def stats(self) -> dict[str, Any]:
        return {
            "l1_messages": len(self._l1_messages),
            "l1_turns": self._l1_turn_count,
            "l1_context_est": self.l1_context_size(),
            "l1_context_limit": self._context_limit,
            "l1_evicts": self._evict_count,
            "l2_tool_cache": len(self._l2_tool_cache),
            "l2_hit_rate": f"{self.l2_hit_rate:.1%}",
            "l2_phase_summaries": len(self._l2_phase_summaries),
            "l3_known_files": self._l3_known_files,
            "l3_accumulated_len": len(self.accumulated_context),
            "cpp_total_tokens": self._cpp.get_total_tokens() if self._cpp else 0,
            "cpp_cache_hits": self._cpp.get_cache_hits() if self._cpp else 0,
            "cpp_cache_misses": self._cpp.get_cache_misses() if self._cpp else 0,
            "tools_used": sorted(set(self._l3_tools_used)),
            "_backend": "cpp" if self._cpp else "python",
        }
