"""Integration tests for WorkflowExecutor (pure Python path).

Uses a mock API client and a mock tool to test the
phase-by-phase execution flow end-to-end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from daoyi.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiTextDeltaEvent,
)
from daoyi.api.usage import UsageSnapshot
from daoyi.config.settings import PermissionSettings
from daoyi.engine.messages import ConversationMessage, TextBlock
from daoyi.engine.stream_events import AssistantTextDelta, ErrorEvent, StatusEvent
from daoyi.permissions.checker import PermissionChecker
from daoyi.permissions.modes import PermissionMode
from daoyi.task_workflow.executor import WorkflowExecutor
from daoyi.task_workflow.models import TaskPhase, TaskWorkflow
from daoyi.tools.base import BaseTool, ToolExecutionContext, ToolRegistry, ToolResult

from pydantic import BaseModel as PydanticBaseModel

# ── Mock tool ──────────────────────────────────────────────────────────


class EchoInput(PydanticBaseModel):
    text: str = ""


class EchoTool(BaseTool):
    """A mock tool that echoes its input."""

    name = "echo"
    description = "Echoes the input text back"
    input_model = EchoInput

    async def execute(self, arguments: PydanticBaseModel, context: ToolExecutionContext) -> ToolResult:
        return ToolResult(output=f"echo: {getattr(arguments, 'text', '')}")


# ── Mock API client ────────────────────────────────────────────────────


class FakeClient:
    """Simulates an LLM API with controllable responses."""

    def __init__(self, responses: list[ConversationMessage] | None = None):
        self._responses = responses or []

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator:
        for msg in self._responses:
            for block in (msg.content if isinstance(msg.content, list) else [msg.content]):
                if hasattr(block, "text") and block.text:
                    yield ApiTextDeltaEvent(text=block.text)
            yield ApiMessageCompleteEvent(
                message=msg,
                usage=UsageSnapshot(input_tokens=10, output_tokens=5),
            )


# ── Helper to build a tool-call message ────────────────────────────────


def _tool_call_msg(tool_name: str, args: dict, tool_use_id: str = "call_1") -> ConversationMessage:
    from daoyi.engine.messages import ToolUseBlock
    return ConversationMessage(
        role="assistant",
        content=[ToolUseBlock(id=tool_use_id, name=tool_name, input=args)],
    )


def _text_msg(text: str) -> ConversationMessage:
    return ConversationMessage(
        role="assistant",
        content=[TextBlock(text=text)],
    )


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def tool_registry():
    r = ToolRegistry()
    r.register(EchoTool())
    return r


@pytest.fixture
def permission_checker():
    return PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))


@pytest.fixture
def simple_workflow():
    return TaskWorkflow(
        id="test_integration_wf",
        trigger_patterns=[r"test.*"],
        description="Integration test workflow",
        phases=[
            TaskPhase(
                name="understand",
                prompt_template="You are a helpful assistant. User request: {user_input}",
                tools=["echo"],
                max_turns=3,
            ),
        ],
    )


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_executor_python_path_happy_path(tool_registry, permission_checker, simple_workflow, tmp_path):
    """Pure Python executor: assistant responds with text (no tool calls), phase completes."""
    client = FakeClient(responses=[_text_msg("Task complete successfully!")])

    executor = WorkflowExecutor(
        api_client=client,
        full_tool_registry=tool_registry,
        permission_checker=permission_checker,
        cwd=tmp_path,
        model="fake-model",
        max_tokens=1024,
    )

    events = []
    async for event in executor.execute(simple_workflow, "test integration"):
        events.append(event)

    assert len(events) > 0
    assert any(isinstance(e, StatusEvent) for e in events)
    assert any(isinstance(e, AssistantTextDelta) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_executor_python_path_tool_then_text(tool_registry, permission_checker, simple_workflow, tmp_path):
    """Assistant calls a tool first, then responds with text."""
    client = FakeClient(responses=[
        _tool_call_msg("echo", {"text": "hello world"}),
        _text_msg("Echoed successfully. Task done."),
    ])

    executor = WorkflowExecutor(
        api_client=client,
        full_tool_registry=tool_registry,
        permission_checker=permission_checker,
        cwd=tmp_path,
        model="fake-model",
        max_tokens=1024,
    )

    events = []
    async for event in executor.execute(simple_workflow, "test tool call"):
        events.append(event)

    assert len(events) > 0
    text_deltas = [e for e in events if isinstance(e, AssistantTextDelta)]
    assert len(text_deltas) >= 1
    assert not any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_executor_cancel_during_execution(tool_registry, permission_checker, simple_workflow, tmp_path):
    """Cancel is called while executor is running — should stop cleanly."""
    import asyncio

    slow_client = FakeClient(responses=[_text_msg("Final answer.")])

    executor = WorkflowExecutor(
        api_client=slow_client,
        full_tool_registry=tool_registry,
        permission_checker=permission_checker,
        cwd=tmp_path,
        model="fake-model",
        max_tokens=1024,
    )

    async def run_and_cancel():
        events = []
        async for event in executor.execute(simple_workflow, "test cancel"):
            events.append(event)
            # Cancel after the first event
            if len(events) == 1:
                executor.cancel()
        return events

    events = await run_and_cancel()
    # Should complete without error even with cancel
    assert len(events) > 0


@pytest.mark.asyncio
async def test_executor_multi_phase(tool_registry, permission_checker, tmp_path):
    """Multiple phases — L3 context passes between phases."""
    wf = TaskWorkflow(
        id="multi_phase_test",
        trigger_patterns=[".*"],
        description="Multi-phase test",
        phases=[
            TaskPhase(name="understand", prompt_template="First phase: {user_input}", tools=["echo"], max_turns=2),
            TaskPhase(name="verify", prompt_template="Second phase: {user_input}\nPrevious: {phase_results}", tools=[], max_turns=2),
        ],
    )

    client = FakeClient(responses=[
        _text_msg("Phase 1 done."),
        _text_msg("Phase 2 done."),
    ])

    executor = WorkflowExecutor(
        api_client=client,
        full_tool_registry=tool_registry,
        permission_checker=permission_checker,
        cwd=tmp_path,
        model="fake-model",
        max_tokens=1024,
    )

    events = []
    async for event in executor.execute(wf, "multi phase test"):
        events.append(event)

    text_deltas = [e for e in events if isinstance(e, AssistantTextDelta)]
    assert len(text_deltas) >= 2  # at least one per phase
    assert not any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_executor_auto_checkpoint_creates_snapshot(tool_registry, permission_checker, simple_workflow, tmp_path):
    """Auto-checkpoint creates a snapshot file on disk."""
    client = FakeClient(responses=[_text_msg("Checkpoint test.")])

    executor = WorkflowExecutor(
        api_client=client,
        full_tool_registry=tool_registry,
        permission_checker=permission_checker,
        cwd=tmp_path,
        model="fake-model",
        max_tokens=1024,
    )
    executor._auto_checkpoint = True

    async for _ in executor.execute(simple_workflow, "test checkpoint"):
        pass

    # A snapshot should have been saved by the auto-checkpoint
    all_snaps = executor._snapshot_mgr.list()
    our_snaps = [s for s in all_snaps if s.workflow_id == "test_integration_wf"]
    assert len(our_snaps) > 0
    assert our_snaps[0].phase_name == "understand"
