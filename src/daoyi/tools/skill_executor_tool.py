"""Tool for dynamically executing CLI commands defined in SKILL.md files."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from daoyi.sandbox import SandboxUnavailableError
from daoyi.skills.loader import get_user_skills_dir, load_skills_from_dirs
from daoyi.skills.types import SkillDefinition
from daoyi.tools.base import BaseTool, ToolExecutionContext, ToolResult
from daoyi.utils.shell import create_shell_subprocess


class SkillExecutorInput(BaseModel):
    """Arguments for executing a skill command."""

    skill_name: str = Field(
        description=(
            "The name of the skill to execute, e.g. 'cli-anything-gimp', 'cli-anything-blender'. "
            "Use 'list' to see all available skills."
        )
    )
    command: str = Field(
        description="The command to run, e.g. 'project new', 'layer add'. "
        "Use 'list <skill_name>' to see available commands for a skill."
    )
    args: str = Field(
        default="",
        description="Additional arguments passed to the command, e.g. '--width 800 --height 600'.",
    )
    timeout_seconds: int = Field(
        default=120,
        ge=1,
        le=600,
        description="Maximum execution time in seconds.",
    )


class CommandDefinition:
    """Parsed command definition from SKILL.md"""

    def __init__(self, name: str, description: str, examples: list[str] | None = None):
        self.name = name
        self.description = description
        self.examples = examples or []


class SkillDefinitionEx:
    """Extended skill definition with parsed commands."""

    def __init__(
        self,
        name: str,
        description: str,
        base_dir: str,
        cli_command: str,
        commands: list[CommandDefinition],
        installation: str | None = None,
    ):
        self.name = name
        self.description = description
        self.base_dir = base_dir
        self.cli_command = cli_command
        self.commands = commands
        self.installation = installation


def parse_skill_md(content: str, base_dir: str) -> SkillDefinitionEx | None:
    """Parse SKILL.md content and extract command definitions."""
    name_match = re.search(r'^name:\s*["\']?([^"\']+)["\']?\s*$', content, re.MULTILINE)
    if not name_match:
        return None

    skill_name = name_match.group(1).strip()

    desc_match = re.search(r'^description:\s*(?:>-\s*\n((?:\s+.+\n?)*))', content, re.MULTILINE)
    if desc_match:
        folded = desc_match.group(1)
        description = re.sub(r'\s+', ' ', folded).strip()
    else:
        inline_match = re.search(r'^description:\s*["\'](.+?)["\']\s*$', content, re.MULTILINE)
        description = inline_match.group(1).strip() if inline_match else ""

    cli_command = skill_name

    installation_match = re.search(r'pip install ([^\s`]+)', content)
    installation = installation_match.group(1) if installation_match else None

    commands: list[CommandDefinition] = []

    command_section = re.search(
        r'## Command[ G]*(?:Groups|)\s*\n\s*\n(.*?)(?=\n##|\n# |\Z)',
        content,
        re.MULTILINE | re.DOTALL,
    )

    if command_section:
        section_text = command_section.group(1)

        table_pattern = re.compile(
            r'\|\s*`([^`]+)`\s*\|\s*([^|]+)\s*\|',
            re.MULTILINE,
        )
        for match in table_pattern.finditer(section_text):
            cmd_name = match.group(1).strip()
            cmd_desc = match.group(2).strip()
            commands.append(CommandDefinition(cmd_name, cmd_desc))

    if not commands:
        subheadings = re.findall(r'###\s+(\w+(?:\s+\w+)*)\s*\n(.*?)(?=\n###|\n##|\Z)', content, re.MULTILINE | re.DOTALL)
        for subheading, subcontent in subheadings:
            if subheading.lower() in ["examples", "usage", "more information"]:
                continue

            table_pattern = re.compile(
                r'\|\s*`([^`]+)`\s*\|\s*([^|]+)\s*\|',
                re.MULTILINE,
            )
            for match in table_pattern.finditer(subcontent):
                cmd_name = match.group(1).strip()
                cmd_desc = match.group(2).strip()
                full_cmd = f"{subheading.lower()} {cmd_name}" if subheading.lower() != skill_name else cmd_name
                commands.append(CommandDefinition(full_cmd, cmd_desc))

    return SkillDefinitionEx(
        name=skill_name,
        description=description,
        base_dir=base_dir,
        cli_command=cli_command,
        commands=commands,
        installation=installation,
    )


def get_skill_commands(skill_name: str) -> SkillDefinitionEx | None:
    """Load and parse a skill by name."""
    skills_dirs = [
        get_user_skills_dir(),
        Path.home() / ".claude" / "skills",
        Path.home() / ".agents" / "skills",
        Path.cwd() / ".daoyi" / "skills",
    ]

    for skill_dir in skills_dirs:
        if not skill_dir.exists():
            continue

        skill_path = skill_dir / skill_name / "SKILL.md"
        if skill_path.exists():
            content = skill_path.read_text(encoding="utf-8")
            return parse_skill_md(content, str(skill_dir / skill_name))

        for child in skill_dir.iterdir():
            if child.is_dir() and child.name == skill_name:
                skill_path = child / "SKILL.md"
                if skill_path.exists():
                    content = skill_path.read_text(encoding="utf-8")
                    return parse_skill_md(content, str(child))

    return None


def list_available_skills() -> list[dict[str, Any]]:
    """List all available skills in user directories."""
    skills_dirs = [
        get_user_skills_dir(),
        Path.home() / ".claude" / "skills",
        Path.home() / ".agents" / "skills",
        Path.cwd() / ".daoyi" / "skills",
    ]

    skills: list[dict[str, Any]] = []

    for skill_dir in skills_dirs:
        if not skill_dir.exists():
            continue

        for child in sorted(skill_dir.iterdir()):
            if not child.is_dir():
                continue

            skill_path = child / "SKILL.md"
            if not skill_path.exists():
                continue

            content = skill_path.read_text(encoding="utf-8")
            parsed = parse_skill_md(content, str(child))

            if parsed:
                skills.append({
                    "name": parsed.name,
                    "description": parsed.description,
                    "commands": [{"name": cmd.name, "description": cmd.description} for cmd in parsed.commands],
                    "cli_command": f"cli-{parsed.name.replace('_', '-')}",
                })

    return skills


class SkillExecutorTool(BaseTool):
    """Dynamically execute CLI commands defined in SKILL.md files."""

    name = "skill_executor"
    description = (
        "Execute CLI commands defined in SKILL.md files. "
        "Supports dynamic skill discovery and command execution. "
        "Use 'list' as skill_name to see all available skills. "
        "Use 'list <skill_name>' to see available commands for a specific skill."
    )
    input_model = SkillExecutorInput

    async def execute(
        self,
        arguments: SkillExecutorInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        if arguments.skill_name == "list":
            return await self._list_skills()

        if arguments.command == "list" and arguments.skill_name != "list":
            return await self._list_skill_commands(arguments.skill_name)

        return await self._execute_skill_command(arguments, context)

    async def _list_skills(self) -> ToolResult:
        """List all available skills."""
        skills = list_available_skills()

        if not skills:
            return ToolResult(
                output="No skills found. Add SKILL.md files to ~/.daoyi/skills/ to get started.",
                is_error=False,
            )

        lines = ["Available Skills:\n"]
        for skill in skills:
            lines.append(f"\n## {skill['name']}")
            lines.append(f"Description: {skill['description']}")
            lines.append(f"CLI Command: {skill['cli_command']}")
            lines.append("Commands:")
            for cmd in skill["commands"][:5]:
                lines.append(f"  - {cmd['name']}: {cmd['description']}")
            if len(skill["commands"]) > 5:
                lines.append(f"  ... and {len(skill['commands']) - 5} more")

        output = "\n".join(lines)
        return ToolResult(output=output, is_error=False)

    async def _list_skill_commands(self, skill_name: str) -> ToolResult:
        """List commands for a specific skill."""
        skill = get_skill_commands(skill_name)

        if not skill:
            return ToolResult(
                output=f"Skill '{skill_name}' not found. Use 'skill_executor list' to see available skills.",
                is_error=True,
            )

        lines = [f"Skill: {skill.name}", f"Description: {skill.description}", "\nCommands:"]

        for cmd in skill.commands:
            lines.append(f"\n  {cmd.name}")
            lines.append(f"    {cmd.description}")

        output = "\n".join(lines)
        return ToolResult(output=output, is_error=False)

    async def _execute_skill_command(
        self,
        arguments: SkillExecutorInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Execute a command from a skill."""
        skill = get_skill_commands(arguments.skill_name)

        if not skill:
            return ToolResult(
                output=f"Skill '{arguments.skill_name}' not found. Use 'skill_executor list' to see available skills.",
                is_error=True,
            )

        if arguments.skill_name == "web_search":
            return await self._execute_web_search(arguments.command, arguments.args, context)

        cli_executable = skill.name

        if not shutil.which(cli_executable):
            install_info = f"pip install {skill.installation or cli_executable}" if skill.installation else f"pip install {cli_executable}"
            return ToolResult(
                output=(
                    f"CLI '{cli_executable}' is not installed.\n"
                    f"Install it with: {install_info}\n"
                    f"\nAvailable commands for {skill.name}:\n"
                    + "\n".join(f"  - {cmd.name}: {cmd.description}" for cmd in skill.commands[:10])
                ),
                is_error=True,
            )

        cmd_parts = [cli_executable, "--json", arguments.command]
        if arguments.args:
            import shlex
            cmd_parts.extend(shlex.split(arguments.args))

        cwd = context.cwd
        try:
            process = await create_shell_subprocess(
                cmd_parts,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except SandboxUnavailableError as exc:
            return ToolResult(output=str(exc), is_error=True)

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=arguments.timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(
                output=(
                    f"{cli_executable} timed out after {arguments.timeout_seconds} seconds"
                ),
                is_error=True,
            )

        stdout_str = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
        stderr_str = stderr.decode("utf-8", errors="replace").strip() if stderr else ""

        if process.returncode != 0:
            body = stderr_str or stdout_str or f"Exit code {process.returncode}"
            return ToolResult(
                output=f"{cli_executable} failed:\n{body}",
                is_error=True,
                metadata={"returncode": process.returncode},
            )

        output = stdout_str or "(no output)"
        return ToolResult(
            output=output,
            is_error=False,
            metadata={"returncode": 0},
        )

    async def _execute_web_search(
        self,
        command: str,
        args: str,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Execute web_search commands through the built-in WebSearchTool."""
        from daoyi.tools.web_search_tool import WebSearchTool
        from daoyi.tools.web_search_tool import WebSearchInput

        web_search_tool = WebSearchTool()

        query_parts = []
        if command == "weather":
            query_parts.append("weather")
        elif command == "news":
            query_parts.append("news")
        elif command == "query":
            pass
        else:
            query_parts.append(command)

        if args:
            query_parts.append(args)

        search_query = " ".join(query_parts).strip()

        if not search_query:
            return ToolResult(
                output="Search query is empty. Please provide a search term.",
                is_error=True,
            )

        input_data = WebSearchInput(query=search_query)
        result = await web_search_tool.execute(input_data, context)
        return result
