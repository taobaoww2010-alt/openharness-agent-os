"""Tests for L3 checkpoint save/load (MemoryManager)."""

from __future__ import annotations

import json
from pathlib import Path

from daoyi.kernel.memory import MemoryManager


def _l3_dir() -> Path:
    return Path.home() / ".daoyi" / "l3"


def test_save_checkpoint_writes_file_with_correct_data():
    mm = MemoryManager(context_window_limit=1024)
    mm.accumulated_context = "phase 1: configured nginx\nphase 2: deployed"
    mm.add_known_file("/etc/nginx/nginx.conf")
    mm.record_tool_use("read_file")
    mm.record_tool_use("write_file")

    mm.save_checkpoint("test-wf-001")

    path = _l3_dir() / "test-wf-001.json"
    assert path.exists(), f"Checkpoint file {path} not written"
    data = json.loads(path.read_text())
    assert data["accumulated_context"] == "phase 1: configured nginx\nphase 2: deployed"
    assert "/etc/nginx/nginx.conf" in data["known_files"]
    assert "write_file" in data["tools_used"]
    assert "updated_at" in data
    path.unlink(missing_ok=True)


def test_load_checkpoint_restores_state():
    mm1 = MemoryManager(context_window_limit=1024)
    mm1.accumulated_context = "initial context"
    mm1.add_known_file("README.md")
    mm1.add_known_file("src/main.py")
    mm1.save_checkpoint("test-wf-load")

    mm2 = MemoryManager(context_window_limit=1024)
    loaded = mm2.load_checkpoint("test-wf-load")

    assert loaded is True
    assert mm2.accumulated_context == "initial context"
    assert "README.md" in mm2.known_files
    assert "src/main.py" in mm2.known_files
    _l3_dir().joinpath("test-wf-load.json").unlink(missing_ok=True)


def test_load_checkpoint_returns_false_when_no_file():
    mm = MemoryManager(context_window_limit=1024)
    loaded = mm.load_checkpoint("nonexistent-workflow")
    assert loaded is False
    assert mm.accumulated_context == ""


def test_load_checkpoint_restores_phase_summaries():
    mm1 = MemoryManager(context_window_limit=1024)
    mm1.accumulated_context = "base context"
    mm1._l2_phase_summaries.append("[Phase init complete]")
    mm1._l2_phase_summaries.append("[Phase build complete]some output")
    mm1.save_checkpoint("test-wf-summaries")

    mm2 = MemoryManager(context_window_limit=1024)
    mm2.load_checkpoint("test-wf-summaries")

    assert "[Phase build complete]" in mm2._l2_phase_summaries[1]
    _l3_dir().joinpath("test-wf-summaries.json").unlink(missing_ok=True)


def test_multiple_saves_overwrite():
    mm = MemoryManager(context_window_limit=1024)
    mm.accumulated_context = "v1"
    mm.save_checkpoint("test-wf-overwrite")
    mm.accumulated_context = "v2"
    mm.save_checkpoint("test-wf-overwrite")

    mm2 = MemoryManager(context_window_limit=1024)
    mm2.load_checkpoint("test-wf-overwrite")
    assert mm2.accumulated_context == "v2"
    _l3_dir().joinpath("test-wf-overwrite.json").unlink(missing_ok=True)


def test_corrupted_checkpoint_returns_false():
    path = _l3_dir() / "test-wf-corrupt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json")

    mm = MemoryManager(context_window_limit=1024)
    loaded = mm.load_checkpoint("test-wf-corrupt")
    assert loaded is False
    path.unlink(missing_ok=True)
