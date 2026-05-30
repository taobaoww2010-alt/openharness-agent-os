"""Context scheduler — select and compress relevant context for the LLM."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from daoyi.kernel.memory import MemoryManager

log = logging.getLogger(__name__)


class ContextScheduler:
    """Selects and compresses context to fit within token budgets.

    Analogous to a memory controller: given a pool of available context
    (all prior messages, tool results, file contents), pick the subset
    most relevant to the current turn and compress anything that exceeds
    the budget.
    """

    def __init__(self, max_context_tokens: int = 4096) -> None:
        self._max_tokens = max_context_tokens

    def select_context(
        self,
        mem: MemoryManager,
        current_phase: str,
        additional_keys: list[str] | None = None,
    ) -> str:
        """Build a compact context string from the memory manager.

        Priority order:
          1. Current phase results (always included)
          2. Tool results from previous phases (if relevant)
          3. Keyword-matched file snippets (if relevant)
          4. Accumulated context (truncated if too long)
        """
        parts: list[str] = []
        budget = self._max_tokens

        # Phase results (highest priority)
        phase_summary = mem.format_phase_results_summary(mem.phase_results)
        if phase_summary:
            budget -= self._estimate_tokens(phase_summary)
            parts.append(phase_summary)

        # Tool results from previous phases
        tool_results = self._select_tool_results(mem)
        if tool_results:
            tr_text = "历史工具结果：\n" + tool_results
            if budget > 0:
                tr_text = self._truncate_to_budget(tr_text, budget)
                budget -= self._estimate_tokens(tr_text)
                parts.append(tr_text)

        # Accumulated context (L3)
        if mem.accumulated_context and budget > 0:
            ctx = self._truncate_to_budget(mem.accumulated_context, budget)
            parts.append(ctx)

        return "\n\n".join(parts)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return len(text) // 4

    @staticmethod
    def _truncate_to_budget(text: str, budget_tokens: int) -> str:
        max_chars = budget_tokens * 4
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n…(truncated)"

    @staticmethod
    def _select_tool_results(mem: MemoryManager) -> str:
        cache = getattr(mem, "_l2_cache", {})
        if not cache:
            return ""
        lines: list[str] = []
        for (tool_name, _), content in list(cache.items())[:5]:
            if isinstance(content, str) and len(content) > 10:
                lines.append(f"[{tool_name}] {content[:200]}")
        return "\n".join(lines)
