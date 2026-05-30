"""Tests for CliAnythingTool."""

from __future__ import annotations

import pytest
from pathlib import Path

from daoyi.tools.base import ToolExecutionContext
from daoyi.tools.cli_anything_tool import CliAnythingTool, CliAnythingInput


class TestCliAnythingTool:
    """Unit tests for the CLI-Anything meta-tool."""

    @pytest.fixture
    def tool(self) -> CliAnythingTool:
        return CliAnythingTool()

    @pytest.fixture
    def context(self) -> ToolExecutionContext:
        return ToolExecutionContext(cwd=Path("/tmp"))

    def test_tool_attributes(self, tool: CliAnythingTool) -> None:
        assert tool.name == "cli_anything"
        assert "gimp" in tool.description
        assert "blender" in tool.description
        assert tool.input_model is CliAnythingInput

    def test_input_model_validation(self) -> None:
        inp = CliAnythingInput(software="gimp", command="image resize", args="--width 800 in.png out.png")
        assert inp.software == "gimp"
        assert inp.command == "image resize"
        assert inp.args == "--width 800 in.png out.png"
        assert inp.timeout_seconds == 120

    def test_input_model_minimal(self) -> None:
        inp = CliAnythingInput(software="blender", command="render")
        assert inp.args == ""
        assert inp.timeout_seconds == 120

    @pytest.mark.asyncio
    async def test_execute_returns_error_when_cli_not_installed(
        self, tool: CliAnythingTool, context: ToolExecutionContext,
    ) -> None:
        inp = CliAnythingInput(software="nonexistent_software_xyz", command="help")
        result = await tool.execute(inp, context)
        assert result.is_error
        assert "not installed" in result.output
        assert "cli-anything-nonexistent_software_xyz" in result.output
