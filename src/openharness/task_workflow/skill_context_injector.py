"""SKILL Context Injector — Inject available SKILL commands into LLM context."""

from __future__ import annotations

from typing import Any

from openharness.task_workflow.skill_discovery import get_skill_matcher


class SkillContextInjector:
    """Inject SKILL information into LLM context for tool discovery."""

    def __init__(self):
        self._matcher = get_skill_matcher()

    def get_skill_tools_for_llm(self, user_intent: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Get SKILL tools formatted for LLM tool calling.

        Returns a list of tool definitions that LLM can use to call SKILL commands.
        """
        if user_intent:
            matched_skills = self._matcher.find_skills(user_intent, limit=limit)
        else:
            matched_skills = self._matcher.list_all_skills()[:limit]

        tools = []
        for skill in matched_skills:
            tool = {
                "type": "function",
                "function": {
                    "name": f"skill_{skill['name']}",
                    "description": f"Execute commands from {skill['name']}. {skill['description']}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": f"The command to execute. Available commands: {', '.join(c['name'] for c in skill['commands'][:5])}",
                            },
                            "args": {
                                "type": "string",
                                "description": "Additional arguments for the command",
                            },
                        },
                        "required": ["command"],
                    },
                },
            }
            tools.append(tool)

        return tools

    def build_skill_context_message(self, user_intent: str | None = None, limit: int = 10) -> dict[str, str]:
        """Build a system message with available SKILL commands.

        This message should be added to the LLM context so it knows what tools are available.
        """
        if user_intent:
            matched_skills = self._matcher.find_skills(user_intent, limit=limit)
            intent_desc = f" for intent: '{user_intent}'"
        else:
            matched_skills = self._matcher.list_all_skills()[:limit]
            intent_desc = ""

        if not matched_skills:
            return {
                "role": "system",
                "content": "No SKILL tools are currently available. Install SKILL packages to enable tool calling.",
            }

        lines = [
            "You have access to the following SKILL tools for executing CLI commands:",
            "",
        ]

        for skill in matched_skills:
            lines.append(f"## {skill['name']}")
            lines.append(f"Description: {skill['description']}")
            lines.append(f"CLI Command: {skill['cli_command']}")
            lines.append("Available commands:")
            for cmd in skill["commands"][:5]:
                lines.append(f"  - {cmd['name']}: {cmd['description']}")
            if len(skill["commands"]) > 5:
                lines.append(f"  ... and {len(skill['commands']) - 5} more commands")
            lines.append("")

        lines.append("---")
        lines.append("Use the skill_executor tool to call these commands when appropriate.")

        return {
            "role": "system",
            "content": "\n".join(lines),
        }

    def get_skill_command_schema(self, skill_name: str) -> dict[str, Any] | None:
        """Get the full command schema for a specific SKILL.

        Returns a tool definition that can be used for structured tool calling.
        """
        skill = self._matcher.get_skill_by_name(skill_name)
        if not skill:
            return None

        properties = {
            "command": {
                "type": "string",
                "description": "The command to execute",
                "enum": [cmd["name"] for cmd in skill["commands"]],
            },
            "args": {
                "type": "string",
                "description": "Additional arguments for the command",
            },
        }

        return {
            "name": "skill_executor",
            "description": f"Execute CLI commands from {skill['name']}. {skill['description']}",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": ["command"],
            },
        }

    def list_all_skills_brief(self) -> list[dict[str, str]]:
        """List all available SKILLs with brief info.

        Returns a list of {name, description, command_count} dicts.
        """
        skills = self._matcher.list_all_skills()
        return [
            {
                "name": s["name"],
                "description": s["description"][:100] + "..." if len(s["description"]) > 100 else s["description"],
                "command_count": len(s["commands"]),
                "cli_command": s["cli_command"],
            }
            for s in skills
        ]


# Module-level singleton
_skill_context_injector: SkillContextInjector | None = None


def get_skill_context_injector() -> SkillContextInjector:
    """Get the global SkillContextInjector instance."""
    global _skill_context_injector
    if _skill_context_injector is None:
        _skill_context_injector = SkillContextInjector()
    return _skill_context_injector
