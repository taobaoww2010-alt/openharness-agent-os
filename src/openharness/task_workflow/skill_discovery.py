"""SKILL-based workflow generator — dynamically create workflows from SKILL.md files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openharness.skills.loader import get_user_skills_dir
from openharness.tools.skill_executor_tool import parse_skill_md


class SkillMatcher:
    """Match user intent to suitable SKILLs based on keyword similarity."""

    def __init__(self, skills_dir: Path | None = None):
        self._skills_dir = skills_dir or get_user_skills_dir()
        self._skills_cache: dict[str, dict[str, Any]] = {}

    def _load_skills(self) -> dict[str, dict[str, Any]]:
        """Load and cache all available SKILLs."""
        if self._skills_cache:
            return self._skills_cache

        skills: dict[str, dict[str, Any]] = {}

        search_dirs = [
            self._skills_dir,
            Path.cwd() / ".openharness" / "skills",
            Path.home() / ".claude" / "skills",
            Path.home() / ".agents" / "skills",
        ]

        for skill_dir in search_dirs:
            if not skill_dir.exists():
                continue

            for child in skill_dir.iterdir():
                if not child.is_dir():
                    continue

                skill_path = child / "SKILL.md"
                if not skill_path.exists():
                    continue

                content = skill_path.read_text(encoding="utf-8")
                parsed = parse_skill_md(content, str(child))

                if parsed and parsed.name not in skills:
                    skills[parsed.name] = {
                        "name": parsed.name,
                        "description": parsed.description,
                        "cli_command": parsed.cli_command,
                        "commands": [
                            {"name": cmd.name, "description": cmd.description}
                            for cmd in parsed.commands
                        ],
                        "base_dir": str(child),
                    }

        self._skills_cache = skills
        return skills

    def find_skills(self, user_intent: str, limit: int = 5) -> list[dict[str, Any]]:
        """Find skills matching the user intent.

        Returns a list of matching skills sorted by relevance score.
        """
        skills = self._load_skills()
        intent_lower = user_intent.lower()

        intent_words = set(re.findall(r'\w+', intent_lower))

        scored_skills: list[tuple[float, dict[str, Any]]] = []

        for skill in skills.values():
            score = 0.0

            desc_lower = skill["description"].lower()
            name_lower = skill["name"].lower()

            for word in intent_words:
                if word in name_lower:
                    score += 3.0
                if word in desc_lower:
                    score += 1.0

            for cmd in skill["commands"]:
                cmd_lower = cmd["description"].lower()
                for word in intent_words:
                    if word in cmd_lower:
                        score += 0.5

            if score > 0:
                scored_skills.append((score, skill))

        scored_skills.sort(key=lambda x: x[0], reverse=True)
        return [skill for _, skill in scored_skills[:limit]]

    def get_skill_by_name(self, name: str) -> dict[str, Any] | None:
        """Get a specific skill by name."""
        skills = self._load_skills()
        return skills.get(name)

    def list_all_skills(self) -> list[dict[str, Any]]:
        """List all available skills."""
        return list(self._load_skills().values())

    def suggest_workflow(self, user_intent: str) -> dict[str, Any]:
        """Suggest a workflow based on user intent.

        Returns a workflow-like structure that can be used by WorkflowExecutor.
        """
        matched_skills = self.find_skills(user_intent, limit=3)

        if not matched_skills:
            return {
                "type": "no_match",
                "message": "No matching skills found. Use 'skill_executor list' to see available skills.",
            }

        primary_skill = matched_skills[0]

        workflow = {
            "type": "skill_based",
            "skill": primary_skill,
            "suggested_commands": primary_skill["commands"][:3],
            "alternatives": matched_skills[1:],
        }

        return workflow


# Module-level singleton
_skill_matcher: SkillMatcher | None = None


def get_skill_matcher() -> SkillMatcher:
    """Get the global SkillMatcher instance."""
    global _skill_matcher
    if _skill_matcher is None:
        _skill_matcher = SkillMatcher()
    return _skill_matcher
