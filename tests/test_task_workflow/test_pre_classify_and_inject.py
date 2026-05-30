"""Tests for pre-classification and /Applications/ injection.

P0.2 — confirm pre-classification routing and app injection work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from daoyi.config.settings import PermissionSettings
from daoyi.permissions.checker import PermissionChecker
from daoyi.permissions.modes import PermissionMode
from daoyi.task_workflow.executor import WorkflowExecutor
from daoyi.task_workflow.models import TaskPhase, TaskWorkflow
from daoyi.tools.base import ToolRegistry


async def _fake_client(req, **kw):
    from daoyi.api.client import ApiMessageCompleteEvent
    from daoyi.api.usage import UsageSnapshot
    from daoyi.engine.messages import ConversationMessage, TextBlock
    await asyncio.sleep(0)
    return ApiMessageCompleteEvent(
        message=ConversationMessage(role="assistant", content=[TextBlock(text="ok")]),
        usage=UsageSnapshot(),
    )


@pytest.fixture
def executor():
    registry = ToolRegistry()
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))
    return WorkflowExecutor(
        api_client=_fake_client,
        full_tool_registry=registry,
        permission_checker=checker,
        cwd=Path.cwd(),
        model="test-model",
        max_tokens=1024,
    )


# ── pre_classify() ──


def test_pre_classify_returns_none_when_no_small_model(executor):
    executor._small_model = None
    executor._classify_with_cpp = MagicMock(return_value=None)
    assert executor.pre_classify("hello") is None
    executor._classify_with_cpp.assert_called_once_with("hello")


def test_pre_classify_delegates_to_small_model(executor):
    mock = MagicMock()
    mock.classify.return_value = "chat"
    executor._small_model = mock
    assert executor.pre_classify("hello") == "chat"
    mock.classify.assert_called_once_with("hello")


def test_pre_classify_returns_none_on_exception(executor):
    mock = MagicMock()
    mock.classify.side_effect = RuntimeError("model crash")
    executor._small_model = mock
    executor._classify_with_cpp = MagicMock(return_value=None)
    assert executor.pre_classify("crash") is None
    executor._classify_with_cpp.assert_called_once_with("crash")


def test_pre_classify_all_intents(executor):
    for intent in ("tool", "chat", "code", "search", "file_ops", "code_review"):
        mock = MagicMock()
        mock.classify.return_value = intent
        executor._small_model = mock
        assert executor.pre_classify("test") == intent


def test_pre_classify_cpp_fallback_when_no_small_model(executor):
    """C++ classifier provides chat intent when small model is unavailable."""
    executor._small_model = None
    from daoyi.kernel import HAS_CPP_CORE
    expected = "chat" if HAS_CPP_CORE else None
    assert executor.pre_classify("hello") == expected


def test_pre_classify_cpp_fallback_when_small_model_crashes(executor):
    """C++ classifier fallback when small model raises."""
    mock = MagicMock()
    mock.classify.side_effect = RuntimeError("crash")
    executor._small_model = mock
    from daoyi.kernel import HAS_CPP_CORE
    expected = "chat" if HAS_CPP_CORE else None
    assert executor.pre_classify("how are you") == expected


# ── /Applications/ injection keyword matching ──


def test_apps_injection_keywords_match():
    from daoyi.task_workflow.executor import _OPEN_KEYWORDS

    matching = ["打开 chrome", "启动微信", "open -a Terminal", "launch safari", "帮我启动应用"]
    for inp in matching:
        assert any(kw in inp for kw in _OPEN_KEYWORDS), f"'{inp}' should match"

    non_matching = ["写代码", "查资料", "运行 rm -rf", "搜索文档", "编辑文件"]
    for inp in non_matching:
        assert not any(kw in inp for kw in _OPEN_KEYWORDS), f"'{inp}' should NOT match"


# ── /Applications/ injection path ──


@pytest.mark.asyncio
async def test_app_injection_with_open_keyword_does_not_crash():
    registry = ToolRegistry()
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))
    exec2 = WorkflowExecutor(
        api_client=_fake_client,
        full_tool_registry=registry,
        permission_checker=checker,
        cwd=Path.cwd(),
        model="test-model",
        max_tokens=1024,
    )
    wf = TaskWorkflow(
        id="test_app_inject",
        trigger_patterns=[r".*"],
        description="app inject test",
        phases=[TaskPhase(name="respond", prompt_template="{user_input}", max_turns=2)],
    )
    events: list = []
    async for ev in exec2.execute(wf, "打开 chrome 浏览器"):
        events.append(ev)
    assert len(events) >= 2


@pytest.mark.asyncio
async def test_app_injection_without_keyword_does_not_crash():
    registry = ToolRegistry()
    checker = PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT))
    exec2 = WorkflowExecutor(
        api_client=_fake_client,
        full_tool_registry=registry,
        permission_checker=checker,
        cwd=Path.cwd(),
        model="test-model",
        max_tokens=1024,
    )
    wf = TaskWorkflow(
        id="test_no_inject",
        trigger_patterns=[r".*"],
        description="no inject test",
        phases=[TaskPhase(name="respond", prompt_template="{user_input}", max_turns=2)],
    )
    events: list = []
    async for ev in exec2.execute(wf, "写一个 hello world"):
        events.append(ev)
    assert len(events) >= 2
