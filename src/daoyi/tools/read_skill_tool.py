"""Tool for reading full SKILL.md content — used by LLM for lazy skill loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from daoyi.skills.loader import get_user_skills_dir
from daoyi.tools.base import BaseTool, ToolExecutionContext, ToolResult
from daoyi.tools.skill_executor_tool import get_skill_commands, list_available_skills


class ReadSkillInput(BaseModel):
    skill_name: str = Field(
        description=(
            "The name of the skill to read, e.g. 'cli-anything-gimp', 'cli-anything-blender'. "
            "Use 'list' to see all available skills."
        )
    )


class ReadSkillTool(BaseTool):
    """Read full SKILL.md content for a specific skill.

    The LLM should use this tool when it needs to see the full command list,
    examples, or installation instructions for a skill listed in `<available-skills>`.
    """

    name = "read_skill"
    description = (
        "Load the full SKILL.md content for a skill, including all available commands, "
        "examples, and installation instructions. Use 'list' as skill_name to list all skills."
    )
    input_model = ReadSkillInput

    async def execute(
        self,
        arguments: ReadSkillInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        if arguments.skill_name == "list":
            return self._list_all_skills()

        skill = get_skill_commands(arguments.skill_name)
        if not skill:
            return ToolResult(
                output=(
                    f"Skill '{arguments.skill_name}' not found. "
                    "Use 'read_skill list' to see available skills."
                ),
                is_error=True,
            )

        lines = [
            f"# {skill.name}",
            f"Description: {skill.description}",
            f"CLI Command: {skill.cli_command}",
            f"Base Directory: {skill.base_dir}",
        ]
        if skill.installation:
            lines.append(f"Installation: pip install {skill.installation}")

        lines.append("")
        lines.append(f"## All Commands ({len(skill.commands)} total)")
        lines.append("")
        lines.append("| Command | Description |")
        lines.append("|---------|-------------|")
        for cmd in skill.commands:
            lines.append(f"| `{cmd.name}` | {cmd.description} |")

        if skill.commands:
            lines.append("")
            lines.append("## Examples")
            for cmd in skill.commands[:3]:
                if cmd.examples:
                    for ex in cmd.examples[:2]:
                        lines.append(f"- `{skill.cli_command} {cmd.name} {ex}`")
                else:
                    lines.append(f"- `{skill.cli_command} {cmd.name} --help`")

        return ToolResult(output="\n".join(lines), is_error=False)

    def _list_all_skills(self) -> ToolResult:
        skills = list_available_skills()
        if not skills:
            return ToolResult(
                output="No skills found. Add SKILL.md files to ~/.daoyi/skills/ to get started.",
                is_error=False,
            )

        lines = ["Available Skills:\n"]
        for s in skills:
            cmd_count = len(s.get("commands", []))
            lines.append(f"- {s['name']}: {s.get('description', '')[:80]} ({cmd_count} commands)")

        return ToolResult(output="\n".join(lines), is_error=False)
