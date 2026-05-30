"""Task classifier — match user input to workflow templates locally (zero LLM cost).

Uses a scoring system instead of first-match:
  - Patterns that are longer / more specific get higher scores.
  - Patterns anchored with ^ get a bonus.
  - The highest-scoring workflow wins.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daoyi.task_workflow.registry import WorkflowRegistry
    from daoyi.task_workflow.models import TaskWorkflow


class TaskClassifier:
    """Lightweight classifier that matches user input to workflows via regex patterns.

    No LLM call is needed — classification is instant and local.
    """

    def __init__(self, registry: WorkflowRegistry) -> None:
        self._registry = registry
        self._compiled: dict[str, list[re.Pattern]] = {}

    def _get_patterns(self, wf_id: str) -> list[re.Pattern]:
        """Lazy-compile regex patterns for a workflow (cached)."""
        if wf_id not in self._compiled:
            wf = self._registry.get(wf_id)
            pats: list[re.Pattern] = []
            if wf:
                for p in wf.trigger_patterns:
                    try:
                        pats.append(re.compile(p, re.IGNORECASE))
                    except re.error:
                        pass
            self._compiled[wf_id] = pats
        return self._compiled[wf_id]

    def classify(self, user_text: str) -> str | None:
        """Return the best-matching workflow id, or *None*."""
        best = self._best_match(user_text)
        return best.id if best else None

    def best_workflow(self, user_text: str) -> TaskWorkflow | None:
        """Return the best-matching workflow object, or *None*."""
        return self._best_match(user_text)

    def _best_match(self, user_text: str) -> TaskWorkflow | None:
        """Score all workflows and return the highest-scoring match."""
        best_wf: TaskWorkflow | None = None
        best_score = 0
        for wf in self._registry.list():
            score = self._score(wf, user_text)
            if score > best_score:
                best_score = score
                best_wf = wf
        return best_wf if best_score > 0 else None

    def _score(self, wf: TaskWorkflow, text: str) -> int:
        """Score how well *wf* matches *text* (higher = better).

        Scoring:
          - Base = pattern length (longer = more specific).
          - Anchor bonus = actual matched prefix chars (proportional, max 20).
          - Exact bonus if pattern has no wildcards (+10).
        """
        total = 0
        for compiled in self._get_patterns(wf.id):
            m = compiled.search(text)
            if not m:
                continue
            pat_str = compiled.pattern
            # Base score = pattern length
            score = len(pat_str)
            # Anchor bonus: proportional to matched prefix length
            if pat_str.startswith("^") and m.start() == 0:
                score += min(len(m.group()), 20)
            # Exact command bonus (no wildcard chars)
            if re.match(r"^[\w/.\-]+$", pat_str):
                score += 10
            total = max(total, score)
        return total

    def suggest_triggers(self, text: str) -> list[str]:
        """Extract potential trigger keywords from a sentence.

        Used by the learner to propose trigger patterns for new workflows.
        """
        stopwords = {
            "a", "an", "the", "is", "are", "was", "were", "be", "been",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "shall", "can", "need",
            "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "我", "你", "他", "她", "它", "们", "的", "了", "是", "在",
            "有", "和", "就", "不", "人", "都", "一", "一个", "上",
            "也", "很", "到", "说", "要", "去", "你", "会", "着",
            "没有", "看", "好", "自己", "这",
        }
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
        keywords = [t for t in tokens if t not in stopwords and len(t) > 1]
        seen: set[str] = set()
        unique: list[str] = []
        for k in keywords:
            if k not in seen:
                seen.add(k)
                unique.append(k)
        return unique
