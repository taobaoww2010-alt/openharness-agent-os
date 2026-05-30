"""SKILL Context Injector — Inject available SKILL commands into LLM context."""

from __future__ import annotations

from daoyi.task_workflow.skill_discovery import get_skill_matcher


class SkillContextInjector:
    """Inject SKILL information into LLM context for tool discovery."""

    def __init__(self):
        self._matcher = get_skill_matcher()

    def build_skill_context_message(self, user_intent: str | None = None, limit: int = 10) -> dict[str, str]:
        """Build a system message with brief available SKILL info.

        Only skill name + one-line description + tier are listed.
        The LLM should use the `read_skill` tool to load full command details
        when it wants to use a specific skill.

        This message should be added to the LLM context so it knows what tools are available.
        """
        if user_intent:
            matched_skills = self._matcher.find_skills(user_intent, limit=limit)
        else:
            matched_skills = self._matcher.list_all_skills()[:limit]

        if not matched_skills:
            return {
                "role": "system",
                "content": "No SKILL tools are currently available. Install SKILL packages to enable tool calling.",
            }

        lines = ["<available-skills>"]
        lines.append(
            "Use `read_skill` tool to load full command list for any skill."
        )

        for skill in matched_skills:
            tier = skill.get("tier", "lite")
            desc = skill["description"]
            if len(desc) > 100:
                desc = desc[:97] + "..."
            lines.append(
                f"- {skill['name']} [{tier}]: {desc} (cli: {skill.get('cli_command', 'N/A')})"
            )

        lines.append("</available-skills>")

        return {
            "role": "system",
            "content": "\n".join(lines),
        }

# Module-level singleton
_skill_context_injector: SkillContextInjector | None = None


def get_skill_context_injector() -> SkillContextInjector:
    """Get the global SkillContextInjector instance."""
    global _skill_context_injector
    if _skill_context_injector is None:
        _skill_context_injector = SkillContextInjector()
    return _skill_context_injector
