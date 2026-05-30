"""Snapshot — 进程快照与恢复。

序列化 workflow 完整状态（Process + MemoryManager），
支持暂停后从快照恢复。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── C++ 核心桥接 ──

try:
    import _daoyi as _CPP
    _HAS_CPP = True
except ImportError:
    _CPP = None
    _HAS_CPP = False


def _snapshot_dir() -> Path:
    base = Path(os.environ.get("DAOYI_HOME", "~/.daoyi")).expanduser()
    path = base / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── Snapshot ──────────────────────────────────────────────────────


class Snapshot:
    """不可变快照数据。"""

    def __init__(
        self,
        snapshot_id: str,
        pid: int,
        workflow_id: str,
        phase_name: str,
        tools_used: list[str],
        cpu_time: float,
        l1_messages: list[dict],
        l2_tool_cache: dict[str, str],
        l2_phase_summaries: list[str],
        l3_known_files: list[str],
        l3_accumulated_context: str,
        created_at: float,
    ) -> None:
        self.snapshot_id = snapshot_id
        self.pid = pid
        self.workflow_id = workflow_id
        self.phase_name = phase_name
        self.tools_used = tools_used
        self.cpu_time = cpu_time
        self.l1_messages = l1_messages
        self.l2_tool_cache = l2_tool_cache
        self.l2_phase_summaries = l2_phase_summaries
        self.l3_known_files = l3_known_files
        self.l3_accumulated_context = l3_accumulated_context
        self.created_at = created_at

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "pid": self.pid,
            "workflow_id": self.workflow_id,
            "phase_name": self.phase_name,
            "tools_used": self.tools_used,
            "cpu_time": self.cpu_time,
            "l1_messages": self.l1_messages,
            "l2_tool_cache": self.l2_tool_cache,
            "l2_phase_summaries": self.l2_phase_summaries,
            "l3_known_files": self.l3_known_files,
            "l3_accumulated_context": self.l3_accumulated_context,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Snapshot:
        return cls(
            snapshot_id=data["snapshot_id"],
            pid=data["pid"],
            workflow_id=data["workflow_id"],
            phase_name=data.get("phase_name", ""),
            tools_used=data.get("tools_used", []),
            cpu_time=data.get("cpu_time", 0.0),
            l1_messages=data.get("l1_messages", []),
            l2_tool_cache=data.get("l2_tool_cache", {}),
            l2_phase_summaries=data.get("l2_phase_summaries", []),
            l3_known_files=data.get("l3_known_files", []),
            l3_accumulated_context=data.get("l3_accumulated_context", ""),
            created_at=data.get("created_at", 0.0),
        )


# ── SnapshotManager ───────────────────────────────────────────────


class SnapshotManager:
    """管理进程快照的创建、保存、列出和恢复。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}
        self._cpp = _CPP.create_snapshot_manager() if _HAS_CPP else None
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """启动时从磁盘加载已有快照。"""
        snap_dir = _snapshot_dir()
        for fpath in sorted(snap_dir.glob("*.json")):
            try:
                with open(fpath) as f:
                    data = json.load(f)
                snap = Snapshot.from_dict(data)
                self._snapshots[snap.snapshot_id] = snap
            except (json.JSONDecodeError, OSError, KeyError) as e:
                log.warning("failed to load snapshot %s: %s", fpath, e)

    def take(
        self,
        pid: int,
        workflow_id: str,
        phase_name: str,
        tools_used: list[str],
        cpu_time: float,
        mem_manager,
    ) -> str:
        """创建当前进程的快照。返回 snapshot_id。

        Args:
            pid: 进程 PID
            workflow_id: workflow 名称
            phase_name: 当前 phase 名称
            tools_used: 已使用的工具列表
            cpu_time: 累计推理时间
            mem_manager: MemoryManager 实例
        """
        snapshot_id = f"snap-{pid}-{int(time.time())}"

        # 序列化 MemoryManager 状态
        l1_serialized = []
        for msg in getattr(mem_manager, "l1_messages", []):
            try:
                l1_serialized.append(_serialize_message(msg))
            except Exception:
                l1_serialized.append({"role": "unknown", "content": str(msg)})

        snap = Snapshot(
            snapshot_id=snapshot_id,
            pid=pid,
            workflow_id=workflow_id,
            phase_name=phase_name,
            tools_used=tools_used,
            cpu_time=cpu_time,
            l1_messages=l1_serialized,
            l2_tool_cache=dict(getattr(mem_manager, "_l2_tool_cache", {})),
            l2_phase_summaries=list(getattr(mem_manager, "_l2_phase_summaries", [])),
            l3_known_files=list(getattr(mem_manager, "_l3_known_files", [])),
            l3_accumulated_context=str(getattr(mem_manager, "_l3_accumulated_context", "")),
            created_at=time.time(),
        )

        # 持久化
        self._snapshots[snapshot_id] = snap
        self._persist(snap)

        log.info("snapshot %s taken for pid=%s (%s)", snapshot_id, pid, workflow_id)
        return snapshot_id

    def get(self, snapshot_id: str) -> Snapshot | None:
        return self._snapshots.get(snapshot_id)

    def list(self) -> list[Snapshot]:
        return sorted(self._snapshots.values(), key=lambda s: s.created_at, reverse=True)

    def delete(self, snapshot_id: str) -> bool:
        snap = self._snapshots.pop(snapshot_id, None)
        if snap is None:
            return False
        fpath = _snapshot_dir() / f"{snapshot_id}.json"
        if fpath.exists():
            fpath.unlink()
        return True

    def restore_to_executor(self, snapshot_id: str, executor) -> bool:
        """从快照恢复 executor 的内存状态。

        设置 MemoryManager 和 Process 的状态到快照时的值。
        返回是否成功。
        """
        snap = self._snapshots.get(snapshot_id)
        if snap is None:
            return False

        mem = executor.memory

        # 恢复 L1 (messages)
        restored = []
        for item in snap.l1_messages:
            try:
                restored.append(_deserialize_message(item))
            except Exception:
                pass
        mem._l1_messages = restored

        # 恢复 L2
        mem._l2_tool_cache = dict(snap.l2_tool_cache)
        mem._l2_phase_summaries = list(snap.l2_phase_summaries)

        # 恢复 L3
        mem._l3_known_files = list(snap.l3_known_files)
        mem._l3_accumulated_context = snap.l3_accumulated_context

        # 恢复进程状态
        proc = getattr(executor, "_proc", None)
        if proc is not None:
            proc.phase_name = snap.phase_name
            proc.tools_used = list(snap.tools_used)
            proc.cpu_time = snap.cpu_time
            proc.state = ProcessState.READY

        log.info(
            "snapshot %s restored to pid=%s (phase=%s, l1=%d, l2_cache=%d, l3_files=%d)",
            snapshot_id, snap.pid, snap.phase_name,
            len(snap.l1_messages), len(snap.l2_tool_cache), len(snap.l3_known_files),
        )
        return True

    def _persist(self, snap: Snapshot) -> None:
        """写入磁盘。"""
        fpath = _snapshot_dir() / f"{snap.snapshot_id}.json"
        with open(fpath, "w") as f:
            json.dump(snap.to_dict(), f, default=str, ensure_ascii=False, indent=2)


# ── 序列化助手 ───────────────────────────────────────────────────


def _serialize_message(msg) -> dict:
    """将 ConversationMessage 序列化为 dict。"""
    if hasattr(msg, "role") and hasattr(msg, "content"):
        content_list = []
        for block in (msg.content if isinstance(msg.content, list) else [msg.content]):
            if hasattr(block, "text"):
                content_list.append({"type": "text", "text": block.text})
            elif hasattr(block, "content"):
                content_list.append({
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": getattr(block, "is_error", False),
                })
            elif hasattr(block, "name"):
                content_list.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input if hasattr(block, "input") else {},
                })
            else:
                content_list.append({"type": "unknown", "val": str(block)})
        return {"role": msg.role, "content": content_list}
    return {"role": "unknown", "content": str(msg)}


def _deserialize_message(data: dict):
    """从 dict 重建 ConversationMessage。"""
    from daoyi.engine.messages import (
        ConversationMessage,
        TextBlock,
        ToolResultBlock,
    )
    content: list = []
    for item in data.get("content", []):
        t = item.get("type", "")
        if t == "text":
            content.append(TextBlock(text=item.get("text", "")))
        elif t == "tool_use":
            from daoyi.engine.messages import ToolUseBlock
            content.append(ToolUseBlock(
                id=item.get("id", ""),
                name=item.get("name", ""),
                input=item.get("input", {}),
            ))
        elif t == "tool_result":
            content.append(ToolResultBlock(
                tool_use_id=item.get("tool_use_id", ""),
                content=item.get("content", ""),
                is_error=item.get("is_error", False),
            ))
    return ConversationMessage(role=data.get("role", "assistant"), content=content)


# ── Forward reference for type hints ─────────────────────────────


from daoyi.kernel.process import ProcessState  # noqa: E402, F811
