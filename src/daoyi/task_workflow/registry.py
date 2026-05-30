"""Workflow template registry — load, save, match, and cache templates."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from daoyi.task_workflow.models import TaskWorkflow

log = logging.getLogger(__name__)


def _registry_dir() -> Path:
    p = Path.home() / ".daoyi" / "workflows"
    p.mkdir(parents=True, exist_ok=True)
    return p


class WorkflowRegistry:
    """Persistent registry of task workflow templates.

    Templates are stored as individual JSON files under
    ``~/.daoyi/workflows/`` for easy portability and manual editing.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self._dir = Path(directory) if directory else _registry_dir()
        self._cache: dict[str, TaskWorkflow] | None = None

    # ── public API ──────────────────────────────────────────────

    def list(self) -> list[TaskWorkflow]:
        """Return all registered workflows."""
        return list(self._load_all().values())

    def get(self, workflow_id: str) -> TaskWorkflow | None:
        return self._load_all().get(workflow_id)

    def find(self, user_text: str) -> TaskWorkflow | None:
        """Return the best-matching workflow (scored), or *None*."""
        from daoyi.task_workflow.classifier import TaskClassifier
        return TaskClassifier(self).best_workflow(user_text)

    def save(self, workflow: TaskWorkflow) -> None:
        workflow.updated_at = __import__("datetime").datetime.now()
        path = self._dir / f"{workflow.id}.json"
        path.write_text(json.dumps(workflow.to_dict(), indent=2, ensure_ascii=False))
        if self._cache is not None:
            self._cache[workflow.id] = workflow
        log.info("saved workflow %s (%d phases)", workflow.id, len(workflow.phases))

    def delete(self, workflow_id: str) -> bool:
        path = self._dir / f"{workflow_id}.json"
        if path.exists():
            path.unlink()
            if self._cache is not None:
                self._cache.pop(workflow_id, None)
            return True
        return False

    def increment_use(self, workflow_id: str) -> None:
        wf = self.get(workflow_id)
        if wf is None:
            return
        wf.use_count += 1
        self.save(wf)

    # ── internals ───────────────────────────────────────────────

    def _load_all(self) -> dict[str, TaskWorkflow]:
        if self._cache is not None:
            return self._cache
        self._cache = {}
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                wf = TaskWorkflow.from_dict(data)
                self._cache[wf.id] = wf
            except Exception as exc:
                log.warning("skipping corrupt workflow %s: %s", path.name, exc)
        return self._cache

    def reload(self) -> None:
        self._cache = None


# module-level singleton for convenience
_default_registry: WorkflowRegistry | None = None


def get_workflow_registry() -> WorkflowRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = WorkflowRegistry()
    return _default_registry
