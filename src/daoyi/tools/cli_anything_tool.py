"""Tool for invoking CLI-Anything professional software CLIs."""

from __future__ import annotations

import asyncio
import json
import shutil
import shlex

from pydantic import BaseModel, Field

from daoyi.sandbox import SandboxUnavailableError
from daoyi.tools.base import BaseTool, ToolExecutionContext, ToolResult
from daoyi.utils.shell import create_shell_subprocess


SUPPORTED_SOFTWARE = [
    "gimp", "inkscape", "krita",
    "blender", "freecad",
    "audacity", "musescore",
    "kdenlive", "shotcut", "obs-studio",
    "libreoffice", "calibre", "zotero",
    "drawio", "mermaid",
    "godot",
    "obsidian",
    "comfyui", "ollama",
    "exa",
    "firefly-iii",
]


class CliAnythingInput(BaseModel):
    """Arguments for invoking a CLI-Anything command."""

    software: str = Field(
        description=(
            "The target software name, e.g. 'gimp', 'blender', 'libreoffice'. "
            "Must be one of the supported software packages."
        )
    )
    command: str = Field(
        description="The CLI command and subcommand to run, e.g. 'image resize', 'document convert'."
    )
    args: str = Field(
        default="",
        description="Additional arguments passed to the CLI command, e.g. '--width 800 input.png output.png'.",
    )
    timeout_seconds: int = Field(
        default=120,
        ge=1,
        le=600,
        description="Maximum execution time in seconds.",
    )


class CliAnythingTool(BaseTool):
    """Invoke a CLI-Anything command for professional software."""

    name = "cli_anything"
    description = (
        "Execute a CLI-Anything command for professional software. "
        "Supported software: " + ", ".join(SUPPORTED_SOFTWARE) + ". "
        "Requires the target software to be installed on the system "
        "and the corresponding cli-anything-<name> pip package."
    )
    input_model = CliAnythingInput

    async def execute(
        self,
        arguments: CliAnythingInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        executable = f"cli-anything-{arguments.software}"
        if not shutil.which(executable):
            return ToolResult(
                output=(
                    f"CLI-Anything tool '{executable}' is not installed. "
                    f"Install it with: pip install {executable}"
                ),
                is_error=True,
            )

        cmd_parts = [executable, "--json", arguments.command] + (
            shlex.split(arguments.args) if arguments.args else []
        )

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
                    f"cli-anything-{arguments.software} timed out "
                    f"after {arguments.timeout_seconds} seconds"
                ),
                is_error=True,
            )

        stdout_str = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
        stderr_str = stderr.decode("utf-8", errors="replace").strip() if stderr else ""

        if process.returncode != 0:
            body = stderr_str or stdout_str or f"Exit code {process.returncode}"
            return ToolResult(
                output=f"{executable} failed:\n{body}",
                is_error=True,
                metadata={"returncode": process.returncode},
            )

        output = stdout_str or "(no output)"
        return ToolResult(
            output=output,
            is_error=False,
            metadata={"returncode": 0},
        )
