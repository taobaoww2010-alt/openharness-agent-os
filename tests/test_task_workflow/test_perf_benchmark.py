"""Performance baseline for multi-phase workflow execution.

Measures latency and peak memory for 3-phase workflows
with varying phase counts and tool-call ratios.

Run with:  python -m pytest tests/test_task_workflow/test_perf_benchmark.py -v --benchmark-stats
"""

from __future__ import annotations

import asyncio
import resource
import sys
import time
from collections.abc import AsyncIterator
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
    name = "echo"
    description = "Echoes the input text back"
    input_model = EchoInput

    async def execute(self, arguments: PydanticBaseModel, context: ToolExecutionContext) -> ToolResult:
        return ToolResult(output=f"echo: {getattr(arguments, 'text', '')}")


# ── Controllable mock client with optional delay ───────────────────────


class FakeClient:
    """Simulates LLM with configurable per-token delay."""

    def __init__(self, responses: list[ConversationMessage] | None = None, delay: float = 0.0):
        self._responses = responses or []
        self._delay = delay

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator:
        for msg in self._responses:
            if self._delay:
                await asyncio.sleep(self._delay)
            for block in (msg.content if isinstance(msg.content, list) else [msg.content]):
                if hasattr(block, "text") and block.text:
                    yield ApiTextDeltaEvent(text=block.text)
            yield ApiMessageCompleteEvent(
                message=msg,
                usage=UsageSnapshot(input_tokens=10, output_tokens=5),
            )


# ── Helpers ────────────────────────────────────────────────────────────


def _text_msg(text: str) -> ConversationMessage:
    return ConversationMessage(role="assistant", content=[TextBlock(text=text)])


def _tool_use_msg(name: str, args: dict | None = None) -> ConversationMessage:
    from daoyi.engine.messages import ToolUseBlock
    return ConversationMessage(
        role="assistant",
        content=[ToolUseBlock(id="call_1", name=name, input=args or {"text": "hello"})],
    )


def _make_workflow(phase_count: int = 3) -> TaskWorkflow:
    phases = [
        TaskPhase(name="research", prompt_template="Research phase", tools=["echo"]),
        TaskPhase(name="implement", prompt_template="Implement phase", tools=["echo"]),
        TaskPhase(name="verify", prompt_template="Verify phase", tools=["echo"]),
    ]
    return TaskWorkflow(
        id="perf-test-3p",
        description="Perf benchmark: 3-phase workflow",
        trigger_patterns=[r".*"],
        phases=phases[:phase_count],
    )


def _make_executor(client, tool_registry) -> WorkflowExecutor:
    checker = PermissionChecker(
        PermissionSettings(mode=PermissionMode.DEFAULT)
    )
    return WorkflowExecutor(
        api_client=client,
        full_tool_registry=tool_registry,
        permission_checker=checker,
        cwd=Path("/tmp"),
        model="fake-model",
        max_tokens=4096,
    )


# ── Shared fixture ─────────────────────────────────────────────────────


@pytest.fixture
def tool_registry():
    reg = ToolRegistry()
    reg.register(EchoTool())
    return reg


# ── Phase-count sweep (no tool calls) ──────────────────────────────────


@pytest.mark.parametrize("num_phases", [1, 3, 5])
@pytest.mark.asyncio
async def test_latency_vs_phase_count(num_phases: int, tool_registry):
    """Measure end-to-end latency for 1/3/5-phase workflows (text-only)."""
    client = FakeClient(responses=[_text_msg(f"Phase {i} done.") for i in range(num_phases)])
    executor = _make_executor(client, tool_registry)
    wf = _make_workflow(num_phases)

    start = time.perf_counter()
    events: list = []
    async for event in executor.execute(wf, "run benchmark", system_prompt_base="You are a helpful assistant."):
        events.append(event)
    elapsed = time.perf_counter() - start

    status_events = [e for e in events if isinstance(e, StatusEvent)]
    print(f"\n  [{num_phases}P] {len(status_events)} status events, {len(events)} total events in {elapsed:.3f}s")
    assert elapsed < 5.0  # should be near-instant with mock


# ── Tool-call vs text-only ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_call_overhead(tool_registry):
    """3-phase: text-only vs tool-call-in-each-phase vs mixed."""
    cases = {
        "text-only": [_text_msg("result")] * 3,
        "tool-each": [_tool_use_msg("echo")] * 3,
        "mixed": [_tool_use_msg("echo"), _text_msg("ok"), _tool_use_msg("echo")],
    }
    for label, responses in cases.items():
        client = FakeClient(responses=responses, delay=0.0)
        executor = _make_executor(client, tool_registry)
        wf = _make_workflow(3)

        start = time.perf_counter()
        events: list = []
        async for event in executor.execute(wf, "run benchmark", system_prompt_base="You are a helpful assistant."):
            events.append(event)
        elapsed = time.perf_counter() - start

        print(f"\n  [{label}] {len(events)} events in {elapsed:.4f}s")
        assert elapsed < 5.0

    print()


# ── Memory baseline ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_usage(tool_registry):
    """Record peak RSS for a 3-phase workflow execution."""
    client = FakeClient(responses=[_tool_use_msg("echo"), _text_msg("ok"), _tool_use_msg("echo")])
    executor = _make_executor(client, tool_registry)
    wf = _make_workflow(3)

    # force GC to get a clean baseline
    import gc; gc.collect()
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    events: list = []
    async for event in executor.execute(wf, "run benchmark", system_prompt_base="You are a helpful assistant."):
        events.append(event)

    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    delta_kb = after - before

    print(f"\n  [memory] max RSS delta={delta_kb:.0f} KB ({delta_kb / 1024:.1f} MB), "
          f"absolute={after / 1024:.0f} KB ({after / 1024 / 1024:.1f} MB)")
    assert delta_kb < 200 * 1024  # sanity: < 200 MB delta


# ── Multi-iteration stability ──────────────────────────────────────────


@pytest.mark.parametrize("iteration", range(5))
@pytest.mark.asyncio
async def test_run_to_run_variance(iteration: int, tool_registry):
    """Capture run-to-run variance over 5 iterations."""
    client = FakeClient(responses=[_text_msg("ok")] * 3)
    executor = _make_executor(client, tool_registry)
    wf = _make_workflow(3)

    start = time.perf_counter()
    events: list = []
    async for event in executor.execute(wf, "run benchmark", system_prompt_base="You are a helpful assistant."):
        events.append(event)
    elapsed = time.perf_counter() - start

    print(f"  [iter {iteration}] {len(events)} events in {elapsed:.4f}s")
    assert elapsed < 5.0
