"""Workflow executor — run a task workflow phase-by-phase.

Each phase creates a **scoped agent** with:
  - Only the tools relevant to that phase (instead of all 43).
  - A focused prompt template (instead of the giant system prompt).
  - A controlled max_turns limit.
  - Per-phase max_tokens to reduce verbose output.

Phase results (especially created/modified files) are tracked and passed
to subsequent phases via MemoryManager (L3 accumulated context).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

if sys.version_info < (3, 11):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _asyncio_timeout(seconds: float) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None:
            yield
            return
        handle = loop.call_later(seconds, task.cancel)
        try:
            yield
        except asyncio.CancelledError:
            raise asyncio.TimeoutError
        finally:
            handle.cancel()
else:
    _asyncio_timeout = asyncio.timeout

from daoyi.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiRetryEvent,
    ApiTextDeltaEvent,
    ApiThinkingDeltaEvent,
    SupportsStreamingMessages,
)
from daoyi.api.usage import UsageSnapshot
from daoyi.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
)
from daoyi.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
    ThinkingDelta,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from daoyi.kernel import HAS_CPP_CORE
from daoyi.kernel.memory import MemoryManager
from daoyi.kernel.process import Process, ProcessState, ProcessTable, Signal
from daoyi.kernel.snapshot import SnapshotManager
from daoyi.kernel.syscall import SyscallTable, Syscall
from daoyi.permissions.checker import PermissionChecker
from daoyi.tools.base import ToolRegistry, ToolExecutionContext

if TYPE_CHECKING:
    from daoyi.task_workflow.models import TaskWorkflow

if HAS_CPP_CORE:
    import _daoyi as _CPP

log = logging.getLogger(__name__)

# ── Per-phase token budgets ───────────────────────────────────────

_PHASE_MAX_TOKENS: dict[str, int] = {
    "understand": 256,
    "search": 256,
    "investigate": 512,
    "analyze": 512,
    "plan": 512,
    "implement": 1024,
    "modify": 1024,
    "generate": 1024,
    "fix": 1024,
    "verify": 256,
    "report": 512,
    "execute": 512,
    "research": 512,
    "discover": 256,
    "detect": 256,
    "install": 512,
    "operate": 512,
    "check_status": 256,
    "summarize": 512,
    "process": 512,
    "run": 512,
}

_PHASE_DEFAULT_MAX_TOKENS = 4096

# Keywords that trigger macOS /Applications/ listing injection
_OPEN_KEYWORDS = ["打开", "启动", "open -a", "launch"]

# ── Error recovery constants ─────────────────────────────────────

_MAX_LLM_RETRIES = 3               # max LLM call retries per turn
_LLM_RETRY_DELAYS = [1.0, 2.0, 4.0]  # exponential backoff (seconds)
_PHASE_TIMEOUT = 120.0             # max seconds per phase
_MAX_CONSECUTIVE_FAILURES = 5      # circuit breaker threshold


# ── C++ tool bridge ─────────────────────────────────────────────

def _run_python_tool_sync(tool_cls, args: dict, loop) -> _CPP.ToolResult:
    """Synchronous bridge: C++ executor → async Python tool."""
    import inspect
    from daoyi.tools.base import ToolExecutionContext

    tool = tool_cls() if isinstance(tool_cls, type) else tool_cls
    sig = inspect.signature(tool.execute)
    params = list(sig.parameters.keys())
    kw = {}
    if "arguments" in params:
        kw["arguments"] = args
    if "context" in params:
        kw["context"] = ToolExecutionContext(arguments=args)
    _new_loop = asyncio.new_event_loop()
    try:
        result = _new_loop.run_until_complete(tool.execute(**kw))
    finally:
        _new_loop.close()
    return _CPP.ToolResult(result.tool_name, result.content, result.is_error)

# ── Executor ──────────────────────────────────────────────────────


class WorkflowExecutor:
    """Execute a workflow template phase-by-phase.

    Uses a MemoryManager for all context/cache/file tracking,
    unified across all phases.
    """

    def __init__(
        self,
        api_client: SupportsStreamingMessages,
        full_tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        cwd: Path,
        model: str,
        max_tokens: int = 4096,
        effort: str | None = None,
        process_table: ProcessTable | None = None,
        phase_timeout: float = _PHASE_TIMEOUT,
        local_model_path: str | None = None,
    ) -> None:
        # Use C++ local LLM engine when a model path is provided
        if local_model_path:
            try:
                from daoyi.api.cpp_client import CppLLMClient
                api_client = CppLLMClient(
                    use_local=True,
                    model_path=local_model_path,
                )
                log.info("Using local LLM: %s", local_model_path)
            except Exception as e:
                log.warning("Failed to init local LLM, using remote: %s", e)
        self._api_client = api_client
        self._full_registry = full_tool_registry
        self._permission_checker = permission_checker
        self._cwd = cwd
        self._model = model
        self._max_tokens = max_tokens
        self._effort = effort
        self._mem = MemoryManager(context_window_limit=max_tokens * 4)
        self._proc_table = process_table or ProcessTable()
        self._proc: Process | None = None
        self._syscall_table = SyscallTable(full_tool_registry)
        self._phase_caps: set[Syscall] | None = None
        self._snapshot_mgr = SnapshotManager()
        self._auto_checkpoint = True
        self._phase_timeout = phase_timeout
        self._consecutive_failures = 0
        self._cancel_requested = False

        # small local model (fast path for chat, optional)
        from daoyi.llm.small_model import SmallModelClient
        self._small_model: SmallModelClient | None = None
        try:
            if SmallModelClient.is_available():
                self._small_model = SmallModelClient.get_instance()
        except Exception:
            self._small_model = None

    @property
    def memory(self) -> MemoryManager:
        return self._mem

    @property
    def process_table(self) -> ProcessTable:
        return self._proc_table

    @property
    def process(self):
        return self._proc

    @property
    def snapshot_manager(self) -> SnapshotManager:
        return self._snapshot_mgr

    def cancel(self) -> None:
        """Request cancellation of the current execution.

        Sends SIGTERM to the process and sets the internal flag.
        """
        self._cancel_requested = True
        pid = self._proc.pid if self._proc else None
        if pid is not None:
            self._proc_table.send_signal(pid, Signal.SIGTERM)

    async def execute(
        self,
        workflow: TaskWorkflow,
        user_input: str,
        *,
        system_prompt_base: str = "You are a helpful AI assistant.",
    ) -> AsyncIterator[StreamEvent]:
        """Run the workflow phases sequentially.

        Yields the same ``StreamEvent`` types as the normal agent loop,
        so the existing TUI / headless renderers work unchanged.
        """

        # ── workspace isolation ──
        _workspace_handle = None
        if not self._cwd.samefile(self._cwd):  # always true; safe path
            try:
                from daoyi.sandbox.workspace_provider import get_workspace_provider

                provider = get_workspace_provider(self._cwd)
                if provider is not None:
                    _workspace_handle = provider.prepare(self._cwd)
                    _original_cwd = self._cwd
                    self._cwd = _workspace_handle.path
                    yield StatusEvent(
                        message=f"[workspace] isolated at {self._cwd} ({provider.id})"
                    )
            except Exception as exc:
                log.warning("Workspace isolation failed, running in-place: %s", exc)

        # ── allocate process ──
        self._proc = self._proc_table.alloc(workflow.id, executor=self)
        self._proc.state = ProcessState.RUNNING
        yield StatusEvent(
            message=f"[kernel] pid={self._proc.pid} workflow={workflow.id} started"
        )

        # Restore L3 from previous session if available
        if self._mem.load_checkpoint(workflow.id):
            yield StatusEvent(message=f"[kernel] L3 context restored for workflow '{workflow.id}'")

        self._mem.accumulated_context = user_input
        phase_results: list[str] = []
        total_start = time.monotonic()

        for phase_idx, phase in enumerate(workflow.phases):
            # check for pending signals
            sig = self._proc_table.pending_signal(self._proc.pid)
            if sig == Signal.SIGTERM:
                self._proc.state = ProcessState.KILLED
                yield StatusEvent(message=f"[kernel] pid={self._proc.pid} received SIGTERM, aborting")
                break
            elif sig == Signal.SIGKILL:
                break

            self._proc.phase_name = phase.name
            self._proc.state = ProcessState.RUNNING
            yield StatusEvent(
                message=(
                    f"[workflow:{workflow.id}] "
                    f"phase {phase_idx + 1}/{len(workflow.phases)}: {phase.name}"
                )
            )

            # ── system prompt ──
            if phase.name in (
                "implement", "fix", "modify", "execute",
                "process", "install", "operate", "run",
            ):
                concise_note = (
                    "\n\n规则：先调用工具完成任务，**不要提前输出解释文字**。"
                    "调用工具后输出一两行结果总结即可。"
                    "用 python3 代替 python 命令。"
                )
            else:
                concise_note = (
                    "\n\n规则：调用工具完成任务后简短总结结果。"
                    "用 python3 代替 python 命令。"
                )
            phase_system_prompt = system_prompt_base + concise_note

            # ── SKILL context injection (brief <available-skills> block) ──
            try:
                from daoyi.task_workflow.skill_context_injector import get_skill_context_injector
                injector = get_skill_context_injector()
                skill_msg = injector.build_skill_context_message(user_input, limit=10)
                phase_system_prompt += "\n\n" + skill_msg["content"]
            except Exception:
                pass  # SKILL injection is optional

            # ── scoped registry (via syscall capabilities) ──
            phase_caps = SyscallTable.caps_for_phase(phase.name)
            self._phase_caps = phase_caps
            scoped_registry = self._syscall_table.build_scoped(
                caps=phase_caps,
                explicit_tools=phase.tools if phase.tools else None,
            )

            # ── add read_skill tool (lazy loading) + skill_executor tool ──
            try:
                from daoyi.tools.read_skill_tool import ReadSkillTool
                scoped_registry.register(ReadSkillTool())
            except Exception:
                pass
            try:
                from daoyi.tools.skill_executor_tool import SkillExecutorTool
                scoped_registry.register(SkillExecutorTool())
            except Exception:
                pass

            # ── phase prompt ──
            files_context = self._mem.make_files_context()
            phase_prompt = phase.prompt_template.format(
                user_input=user_input,
                cwd=str(self._cwd),
                accumulated_context=self._mem.accumulated_context,
                phase_results=self._mem.format_phase_results_summary(phase_results),
            )
            if files_context:
                phase_prompt = files_context + phase_prompt

            # Pre-check: if user wants to open an app, list /Applications/ and
            # inject into the prompt so the model knows exact app names.
            if any(kw in user_input for kw in _OPEN_KEYWORDS):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ls", "/Applications/",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                    apps = stdout.decode().strip()
                    if apps:
                        phase_prompt += (
                            f"\n\n当前系统已安装的应用：\n{apps}\n"
                            "请从以上列表中选择正确的 App 名称。"
                        )
                except (asyncio.TimeoutError, FileNotFoundError, OSError):
                    pass

            initial_messages: list[ConversationMessage] = [
                ConversationMessage(
                    role="user",
                    content=[TextBlock(text=phase_prompt)],
                )
            ]
            self._mem.l1_set_initial(initial_messages)

            # ── per-phase token budget ──
            phase_max_tokens = _PHASE_MAX_TOKENS.get(
                phase.name, _PHASE_DEFAULT_MAX_TOKENS
            )

            # ── scoped agent loop ──
            turn_count = 0
            phase_timed_out = False
            while turn_count < phase.max_turns:
                turn_count += 1
                final_message: ConversationMessage | None = None

                # Circuit breaker: too many consecutive failures
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    yield ErrorEvent(
                        message=f"Circuit breaker: {self._consecutive_failures} consecutive failures"
                    )
                    break

                # ── LLM call with retry + phase timeout ──
                llm_ok = False
                for attempt in range(_MAX_LLM_RETRIES):
                    if attempt > 0:
                        delay = _LLM_RETRY_DELAYS[min(attempt - 1, len(_LLM_RETRY_DELAYS) - 1)]
                        yield StatusEvent(
                            message=f"LLM retry {attempt}/{_MAX_LLM_RETRIES} after {delay:.0f}s"
                        )
                        await asyncio.sleep(delay)

                    try:
                        self._proc.state = ProcessState.BLOCKED
                        t0 = time.monotonic()

                        # Stream LLM response with phase timeout
                        final_message = None
                        async with _asyncio_timeout(self._phase_timeout):
                            async for event in self._api_client.stream_message(
                                ApiMessageRequest(
                                    model=self._model,
                                    messages=self._mem.l1_messages,
                                    system_prompt=phase_system_prompt,
                                    max_tokens=phase_max_tokens,
                                    tools=scoped_registry.to_api_schema(),
                                    effort=self._effort,
                                )
                            ):
                                if isinstance(event, ApiTextDeltaEvent):
                                    yield AssistantTextDelta(text=event.text)
                                elif isinstance(event, ApiThinkingDeltaEvent):
                                    yield ThinkingDelta(text=event.text)
                                elif isinstance(event, ApiRetryEvent):
                                    yield StatusEvent(
                                        message=(
                                            f"Retry ({event.attempt + 1}/"
                                            f"{event.max_attempts}): {event.message}"
                                        )
                                    )
                                elif isinstance(event, ApiMessageCompleteEvent):
                                    final_message = event.message

                        self._proc_table.add_cpu_time(self._proc.pid, time.monotonic() - t0)
                        self._proc.state = ProcessState.RUNNING
                        self._consecutive_failures = 0
                        llm_ok = True
                        break

                    except asyncio.TimeoutError:
                        yield ErrorEvent(
                            message=f"Phase '{phase.name}' timed out after {self._phase_timeout:.0f}s"
                        )
                        phase_timed_out = True
                        break

                    except Exception as exc:
                        log.warning("LLM call attempt %d failed: %s", attempt + 1, exc)
                        self._consecutive_failures += 1
                        if attempt < _MAX_LLM_RETRIES - 1:
                            continue
                        yield ErrorEvent(message=f"LLM call failed after {_MAX_LLM_RETRIES} attempts: {exc}")
                        break

                if phase_timed_out:
                    break
                if not llm_ok:
                    break

                if final_message is None or final_message.is_effectively_empty():
                    break

                yield AssistantTurnComplete(
                    message=final_message, usage=UsageSnapshot()
                )

                # check for tool calls
                tool_uses = final_message.tool_uses
                if not tool_uses:
                    phase_results.append(final_message.text or "")
                    break

                # execute tools — parallel if multiple
                tool_results: list[ToolResultBlock] = []
                self._proc.state = ProcessState.BLOCKED

                # emit start events before launching (for UI responsiveness)
                for tc in tool_uses:
                    yield ToolExecutionStarted(
                        tool_name=tc.name, tool_input=tc.input
                    )

                async def _exec_one(tc) -> tuple[str, ToolResultBlock]:
                    return tc.id, await self._execute_one_tool(tc, scoped_registry)

                # stream completions as each tool finishes, but keep ordered results
                fut_map = {
                    asyncio.ensure_future(_exec_one(tc)): tc for tc in tool_uses
                }
                result_map: dict[str, ToolResultBlock] = {}
                while fut_map:
                    done, _ = await asyncio.wait(fut_map.keys(), return_when=asyncio.FIRST_COMPLETED)
                    for fut in done:
                        tc = fut_map.pop(fut)
                        try:
                            tid, block = await fut
                        except Exception as exc:
                            block = ToolResultBlock(
                                tool_use_id=tc.id,
                                content=f"Error: {exc}",
                                is_error=True,
                            )
                        if isinstance(block, BaseException):
                            block = ToolResultBlock(
                                tool_use_id=tc.id,
                                content=f"Error: {block}",
                                is_error=True,
                            )
                        result_map[tc.id] = block
                        yield ToolExecutionCompleted(
                            tool_name=tc.name,
                            output=self._mem.truncate(block.content),
                            is_error=block.is_error,
                        )

                # reconstruct in original order for L1 consistency
                tool_results = [result_map[tc.id] for tc in tool_uses]

                # L1: append turn (will auto-evict if needed)
                self._mem.l1_append_turn(final_message, tool_results)
                self._proc.state = ProcessState.RUNNING

            # ── phase complete: squash + checkpoint ──
            self._mem.squash_for_next_phase(phase.name, phase_results, workflow_id=workflow.id)
            if self._auto_checkpoint and self._proc:
                try:
                    self._snapshot_mgr.take(
                        pid=self._proc.pid,
                        workflow_id=workflow.id,
                        phase_name=phase.name,
                        tools_used=self._mem.tools_used,
                        cpu_time=getattr(self._proc, "cpu_time", 0.0),
                        mem_manager=self._mem,
                    )
                except Exception as e:
                    log.warning("auto-checkpoint failed: %s", e)

        # ── workspace cleanup ──
        if _workspace_handle is not None:
            try:
                _workspace_handle.cleanup()
                yield StatusEvent(message=f"[workspace] cleaned up {_workspace_handle.path}")
            except Exception as exc:
                log.warning("Workspace cleanup failed: %s", exc)
            self._cwd = _original_cwd

        total_duration = time.monotonic() - total_start
        if self._proc and self._proc.state != ProcessState.KILLED:
            self._proc.state = ProcessState.DONE
        self._proc_table.attach_mem_stats(self._proc.pid, self._mem.stats())

        # flush replay trace if available
        if hasattr(self._api_client, "flush_trace"):
            try:
                trace_path = self._api_client.flush_trace()
                if trace_path:
                    log.info("replay trace saved to %s", trace_path)
            except Exception:
                pass
        yield StatusEvent(
            message=(
                f"[workflow:{workflow.id}] completed in {total_duration:.1f}s "
                f"({len(workflow.phases)} phases, "
                f"tools used: {', '.join(sorted(set(self._mem.tools_used)))})"
            )
        )

    # ── DEPRECATED — C++ executor path ──────────────────────────
    #
    # _execute_with_cpp and _cpp_convert_events are preserved for
    # reference only. The C++ full-executor path is abandoned due
    # to GIL+std::async deadlock. Python execute() runs via pure
    # Python path (above). These methods are no longer called.
    # ────────────────────────────────────────────────────────────

    def _make_cpp_llm(self):
        """DEPRECATED — returns None. Previously created C++ LLMInterface."""
        return None

    async def _execute_with_cpp(
        self,
        workflow: TaskWorkflow,
        user_input: str,
        *,
        system_prompt_base: str = "You are a helpful AI assistant.",
    ) -> AsyncIterator[StreamEvent]:
        """DEPRECATED — C++ full-executor path is abandoned.
        Previously delegated the full workflow to C++ executor.
        Now raises RuntimeError if somehow called.
        """
        raise RuntimeError("_execute_with_cpp is deprecated and no longer available")

    def _cpp_convert_events(
        self,
        events: list[dict],
        flush_turn_fn,
    ) -> list[StreamEvent]:
        """DEPRECATED — C++ full-executor path is abandoned.
        Previously converted C++ event dicts to Python StreamEvents.
        """
        return []

    # ── Pre-classification ──────────────────────────────────────────

    _INTENT_MAP = {
        1: "chat",      # CHAT
        2: "tool",      # RUN_COMMAND
        3: "file_ops",  # WRITE_FILE
        4: "file_ops",  # READ_FILE
        5: "search",    # SEARCH
        6: "code",      # CODE_GENERATION
        7: "tool",      # WORKFLOW
        8: "tool",      # TOOL_CALL
    }

    def _classify_with_python_rules(self, user_input: str) -> str | None:
        """Hot-reloadable Python rules (no C++ compilation needed)."""
        from daoyi.llm.classifier import RuleClassifier
        try:
            return RuleClassifier().classify(user_input)
        except Exception:
            return None

    def _classify_with_cpp(self, user_input: str) -> str | None:
        """Fast regex-based classification via C++ ClassifierInterface (fallback)."""
        if not HAS_CPP_CORE:
            return None
        try:
            cpp_cls = _CPP.create_classifier()
            intent = int(cpp_cls.classify(user_input))
            conf = cpp_cls.get_confidence()
            if conf < 0.7 or intent == 0:
                return None
            return self._INTENT_MAP.get(intent)
        except Exception:
            return None

    def pre_classify(self, user_input: str, previous_intent: str | None = None) -> str | None:
        """Classify user intent.

        Priority:
        1. Short continuation detection (if matches and previous_intent is set)
        2. SmallModelClient (LLM, accurate)
        3. Python rules (hot-reloadable)
        4. C++ (fallback)

        Returns ``"tool"``, ``"chat"``, ``"code"``, ``"search"``,
        ``"file_ops"``, or ``"code_review"`` — or *None* if unavailable.
        """
        # Short continuation: inherit previous intent without re-classifying
        if previous_intent is not None:
            from daoyi.llm.classifier import is_short_continuation
            if is_short_continuation(user_input):
                return previous_intent

        if self._small_model is not None:
            try:
                return self._small_model.classify(user_input)
            except Exception:
                pass
        py_result = self._classify_with_python_rules(user_input)
        if py_result is not None:
            return py_result
        return self._classify_with_cpp(user_input)

    async def chat(
        self,
        user_input: str,
        model: str,
    ) -> AsyncIterator[StreamEvent]:
        """Fast chat path — no tools, minimal system prompt.

        Uses local small model as fast path when available (no API call,
        no latency). Falls back to the remote API client.

        Yields the same StreamEvent types as normal agent loop,
        so the render path works unchanged. Returns bool indicating
        whether the response was purely textual (True) or the model
        tried to use tools (False — caller should retry with full engine).
        """
        from daoyi.engine.messages import TextBlock

        # ── remote API path (small model is only used for pre_classify, not for generation) ──
        chat_system = (
            "You are a helpful assistant. Answer concisely. "
            "You do NOT have access to any tools or commands."
        )
        chat_messages = [
            ConversationMessage(
                role="user",
                content=[TextBlock(text=user_input)],
            )
        ]

        final_message = None
        try:
            async for event in self._api_client.stream_message(
                ApiMessageRequest(
                    model=model,
                    messages=chat_messages,
                    system_prompt=chat_system,
                    max_tokens=1024,
                    tools=[],
                )
            ):
                if isinstance(event, ApiTextDeltaEvent):
                    yield AssistantTextDelta(text=event.text)
                elif isinstance(event, ApiThinkingDeltaEvent):
                    yield ThinkingDelta(text=event.text)
                elif isinstance(event, ApiMessageCompleteEvent):
                    final_message = event.message
        except Exception as exc:
            yield ErrorEvent(message=f"Chat error: {exc}")
            return

        if final_message is None:
            return

        # Check if model tried to use tools (shouldn't happen with tools=[],
        # but some models might)
        if final_message.tool_uses:
            yield StatusEvent(
                message="[chat] model requested tools — retrying with full engine"
            )
            return

        # Safety net: if user query looks like an action and model refused,
        # retry with full engine so it has tools.
        response_text = final_message.text.strip()
        refusal_hints = ("无法直接", "无法执行", "不能", "没有权限", "无法",
                         "don't have access", "no tools", "can't perform",
                         "cannot open", "cannot execute")
        action_hints = ("打开", "启动", "关闭", "运行", "执行", "创建",
                        "删除", "搜索", "下载", "安装",
                        "open", "launch", "start", "run", "execute")
        is_refusal = any(h in response_text for h in refusal_hints)
        is_action = any(h in user_input for h in action_hints)
        if is_refusal and is_action:
            yield StatusEvent(
                message="[chat] model refused action — retrying with full engine"
            )
            return

        yield AssistantTurnComplete(
            message=final_message,
            usage=UsageSnapshot(),
        )
        yield StatusEvent(message="[chat] completed")

    async def _execute_one_tool(
        self,
        tool_call,
        scoped_registry: ToolRegistry,
    ) -> ToolResultBlock:
        """Execute a single tool call (with caching via SyscallTable)."""
        name = tool_call.name
        inp = tool_call.input if isinstance(tool_call.input, dict) else {}
        self._mem.record_tool_use(name)
        if self._proc:
            self._proc_table.add_tool(self._proc.pid, name)

        # Check L2 cache
        cached = self._mem.get_cached_tool_result(name, inp, tool_call.id)
        if cached is not None:
            return cached

        # Syscall audit
        blocked, tool = await self._syscall_table.dispatch_with_audit(
            name, inp, tool_call.id, scoped_registry, self,
        )
        if blocked is not None:
            return blocked
        if tool is None:
            return ToolResultBlock(
                tool_use_id=tool_call.id,
                content=f"Error: tool '{name}' not available in this phase",
                is_error=True,
            )

        try:
            input_model = getattr(tool, "input_model", None)
            if input_model is not None:
                parsed = input_model.model_validate(inp)
            else:
                parsed = inp
            exec_context = ToolExecutionContext(cwd=self._cwd)
            result = await tool.execute(parsed, exec_context)
            block = ToolResultBlock(
                tool_use_id=tool_call.id,
                content=result.output,
                is_error=result.is_error,
            )
            # Track created files
            if name == "write_file" and not result.is_error:
                fp = inp.get("file_path") or inp.get("path", "")
                self._mem.add_known_file(fp)
            # Store in L2 cache
            self._mem.set_cached_tool_result(name, inp, result.output)
            return block
        except Exception as exc:
            return ToolResultBlock(
                tool_use_id=tool_call.id,
                content=f"Error: {exc}",
                is_error=True,
            )
