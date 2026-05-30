"""Process model — workflow 实例 = OS 进程。

每个 workflow 获得唯一 PID，支持信号和状态跟踪。
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daoyi.task_workflow.executor import WorkflowExecutor

log = logging.getLogger(__name__)

# ── C++ 核心桥接 ──

try:
    import _daoyi as _CPP
    _HAS_CPP = True
except ImportError:
    _CPP = None
    _HAS_CPP = False


def _cpp_state(py_state: "ProcessState"):
    """Python ProcessState → C++ ProcessState."""
    if _HAS_CPP:
        m = {
            "ready": _CPP.ProcessState.READY,
            "running": _CPP.ProcessState.RUNNING,
            "blocked": _CPP.ProcessState.BLOCKED,
            "done": _CPP.ProcessState.DONE,
            "killed": _CPP.ProcessState.KILLED,
        }
        return m.get(py_state.value if hasattr(py_state, "value") else str(py_state).lower(),
                     _CPP.ProcessState.READY)
    return None

def _cpp_signal(py_sig: "Signal"):
    """Python Signal → C++ Signal."""
    if _HAS_CPP:
        m = {
            Signal.NONE: _CPP.Signal.NONE,
            Signal.SIGINT: _CPP.Signal.INTERRUPT,
            Signal.SIGTERM: _CPP.Signal.TERMINATE,
            Signal.SIGKILL: _CPP.Signal.KILL,
        }
        return m.get(py_sig, _CPP.Signal.NONE)
    return None


# ── Process State ─────────────────────────────────────────────────


class ProcessState(enum.Enum):
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"  # 等待工具 / LLM 结果
    DONE = "done"
    KILLED = "killed"


# ── Signals ───────────────────────────────────────────────────────


class Signal(enum.IntEnum):
    NONE = 0
    SIGINT = 2   # Ctrl+C — 中断当前 LLM 调用
    SIGTERM = 15 # 优雅终止
    SIGKILL = 9  # 强制终止
    SIGUSR1 = 10 # 刷新缓存


# ── Process ───────────────────────────────────────────────────────


@dataclass
class Process:
    pid: int
    workflow_id: str
    state: ProcessState = ProcessState.READY
    phase_name: str = ""
    start_time: float = 0.0
    cpu_time: float = 0.0  # accumulated LLM inference wall time
    tools_used: list[str] = field(default_factory=list)
    mem_stats: dict | None = None  # snapshot of MemoryManager.stats()

    @property
    def elapsed(self) -> float:
        if self.start_time == 0:
            return 0.0
        return time.monotonic() - self.start_time

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "workflow_id": self.workflow_id,
            "state": self.state.value,
            "phase": self.phase_name,
            "elapsed_s": round(self.elapsed, 1),
            "cpu_time_s": round(self.cpu_time, 1),
            "tools_used": ", ".join(sorted(set(self.tools_used))),
        }


# ── Process Table ─────────────────────────────────────────────────


class ProcessTable:
    """全局进程表。一个 Agent 实例持有一张表。"""

    def __init__(self) -> None:
        self._next_pid = 1
        self._processes: dict[int, Process] = {}
        self._signals: dict[int, Signal] = {}  # pending signals per PID
        self._cpp = _CPP.create_process_manager() if _HAS_CPP else None

    # ── 生命周期 ──

    def alloc(
        self, workflow_id: str, executor: WorkflowExecutor | None = None
    ) -> Process:
        """分配新 PID 并注册进程。"""
        if self._cpp:
            pid = self._cpp.allocate(0)
        else:
            pid = self._next_pid
            self._next_pid += 1
        proc = Process(
            pid=pid,
            workflow_id=workflow_id,
            start_time=time.monotonic(),
        )
        self._processes[pid] = proc
        return proc

    def free(self, pid: int) -> None:
        """释放进程（从表中移除）。"""
        if self._cpp:
            self._cpp.release(pid)
        self._processes.pop(pid, None)
        self._signals.pop(pid, None)

    def get(self, pid: int) -> Process | None:
        return self._processes.get(pid)

    def list(self) -> list[Process]:
        return list(self._processes.values())

    # ── 状态变更 ──

    def set_state(self, pid: int, state: ProcessState) -> None:
        if self._cpp:
            self._cpp.update_state(pid, _cpp_state(state))
        proc = self._processes.get(pid)
        if proc:
            proc.state = state

    def set_phase(self, pid: int, phase_name: str) -> None:
        if self._cpp:
            self._cpp.set_phase(pid, phase_name)
        proc = self._processes.get(pid)
        if proc:
            proc.phase_name = phase_name

    def add_tool(self, pid: int, tool_name: str) -> None:
        if self._cpp:
            self._cpp.add_tool_use(pid, tool_name)
        proc = self._processes.get(pid)
        if proc and tool_name not in proc.tools_used:
            proc.tools_used.append(tool_name)

    def add_cpu_time(self, pid: int, seconds: float) -> None:
        if self._cpp:
            self._cpp.add_cpu_time(pid, seconds)
        proc = self._processes.get(pid)
        if proc:
            proc.cpu_time += seconds

    def attach_mem_stats(self, pid: int, stats: dict) -> None:
        proc = self._processes.get(pid)
        if proc:
            proc.mem_stats = stats

    # ── 信号 ──

    def send_signal(self, pid: int, sig: Signal) -> bool:
        """发送信号到进程。返回进程是否存在。"""
        if pid not in self._processes:
            return False
        if self._cpp:
            self._cpp.send_signal(pid, _cpp_signal(sig))
        self._signals[pid] = sig
        if sig == Signal.SIGKILL:
            proc = self._processes[pid]
            proc.state = ProcessState.KILLED
        return True

    def pending_signal(self, pid: int) -> Signal:
        """取走进程的待处理信号（消费后清空）。"""
        if self._cpp:
            csig = self._cpp.pending_signal(pid)
            if csig != _CPP.Signal.NONE:
                _sig = next(
                    (k for k, v in {
                        Signal.NONE: _CPP.Signal.NONE,
                        Signal.SIGINT: _CPP.Signal.INTERRUPT,
                        Signal.SIGTERM: _CPP.Signal.TERMINATE,
                        Signal.SIGKILL: _CPP.Signal.KILL,
                    }.items() if v == csig),
                    Signal.NONE,
                )
                self._signals[pid] = _sig
        return self._signals.pop(pid, Signal.NONE)

    def has_pending_signal(self, pid: int) -> bool:
        if self._cpp:
            return self._cpp.pending_signal(pid) != _CPP.Signal.NONE
        return pid in self._signals

    # ── 工具函数 ──

    def format_ps(self) -> str:
        """类 ps 的进程列表输出。"""
        if not self._processes:
            return "  (no processes)"

        header = f"{'PID':>4}  {'WORKFLOW':<20}  {'STATE':<10}  {'PHASE':<15}  {'TIME':>6}  {'TOOLS'}"
        sep = "────  ────────────────────  ──────────  ───────────────  ──────  ─────────────────────"
        lines = [header, sep]
        for proc in sorted(self._processes.values(), key=lambda p: p.pid):
            lines.append(
                f"{proc.pid:>4}  {proc.workflow_id:<20}  "
                f"{proc.state.value:<10}  {proc.phase_name:<15}  "
                f"{proc.elapsed:>5.1f}s  "
                f"{', '.join(sorted(set(proc.tools_used)))[:30]}"
            )
        return "\n".join(lines)
