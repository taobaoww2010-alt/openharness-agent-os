"""Data models for task workflow templates."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class TaskPhase:
    """A single phase in a task workflow.

    Each phase sends a *scoped* request to the LLM:
      - Only ``tools`` are registered (not all 43).
      - ``prompt_template`` is the phase instruction (not the giant system prompt).
      - ``max_turns`` limits how many agent loops this phase can take.
    """

    name: str
    prompt_template: str
    tools: list[str] = field(default_factory=list)
    max_turns: int = 20


@dataclass
class TaskWorkflow:
    """A reusable execution plan for a category of task."""

    id: str
    trigger_patterns: list[str]  # regex patterns matched against user input
    description: str
    phases: list[TaskPhase] = field(default_factory=list)

    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    use_count: int = 0

    # Learning metadata
    source_model: str = ""
    tools_observed: list[str] = field(default_factory=list)
    avg_duration_seconds: float = 0.0

    def matches(self, text: str) -> bool:
        for pattern in self.trigger_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trigger_patterns": self.trigger_patterns,
            "description": self.description,
            "phases": [
                {
                    "name": p.name,
                    "prompt_template": p.prompt_template,
                    "tools": p.tools,
                    "max_turns": p.max_turns,
                }
                for p in self.phases
            ],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "use_count": self.use_count,
            "source_model": self.source_model,
            "tools_observed": self.tools_observed,
            "avg_duration_seconds": self.avg_duration_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskWorkflow:
        return cls(
            id=d["id"],
            trigger_patterns=d["trigger_patterns"],
            description=d.get("description", ""),
            phases=[TaskPhase(**p) for p in d.get("phases", [])],
            created_at=datetime.fromisoformat(d["created_at"]) if "created_at" in d else datetime.now(),
            updated_at=datetime.fromisoformat(d["updated_at"]) if "updated_at" in d else datetime.now(),
            use_count=d.get("use_count", 0),
            source_model=d.get("source_model", ""),
            tools_observed=d.get("tools_observed", []),
            avg_duration_seconds=d.get("avg_duration_seconds", 0.0),
        )
