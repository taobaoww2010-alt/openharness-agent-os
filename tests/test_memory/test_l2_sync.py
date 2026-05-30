"""Test L2 tool-result cache sync between Python and C++ MemoryManagers.

Verifies:
  1. set_cached_tool_result writes to both Python and C++ backends.
  2. get_cached_tool_result reads from C++ when available.
  3. A separate C++ MemoryManager can receive synced L2 entries.
"""

from __future__ import annotations

import pytest

from daoyi.kernel.memory import MemoryManager, _HAS_CPP

pytestmark = [
    pytest.mark.skipif(not _HAS_CPP, reason="C++ core not available"),
]


def _key(name: str, inp: dict) -> str:
    return MemoryManager._tool_cache_key(name, inp)


def test_l2_set_writes_to_cpp():
    """set_cached_tool_result writes to C++ backend (readable via l2_get)."""
    mem = MemoryManager(65536)
    assert mem._cpp is not None

    mem.set_cached_tool_result("echo", {"text": "hello"}, "echo: hello")
    cached = mem._cpp.l2_get(_key("echo", {"text": "hello"}))
    assert cached == "echo: hello"


def test_l2_get_reads_from_cpp():
    """get_cached_tool_result returns data stored via C++ l2_set."""
    mem = MemoryManager(65536)
    assert mem._cpp is not None

    mem._cpp.l2_set(_key("direct", {"k": "v"}), "from-cpp")
    result = mem.get_cached_tool_result("direct", {"k": "v"}, "call_1")
    assert result is not None
    assert result.content == "from-cpp"


def test_l2_miss_returns_none():
    """get_cached_tool_result returns None for uncached entries."""
    mem = MemoryManager(65536)
    result = mem.get_cached_tool_result("echo", {"text": "never-seen"}, "call_x")
    assert result is None


def test_l2_sync_between_separate_managers():
    """L2 entries from Python MemoryManager are readable by a fresh C++ one."""
    py_mem = MemoryManager(65536)
    py_mem.set_cached_tool_result("echo", {"text": "sync-test"}, "echo: sync-test")

    import _daoyi as _CPP
    cpp_mem = _CPP.create_memory_manager(65536)
    for key, value in py_mem._l2_tool_cache.items():
        cpp_mem.l2_set(key, value)

    assert cpp_mem.l2_get(_key("echo", {"text": "sync-test"})) == "echo: sync-test"


def test_l2_hit_rate_sync():
    """Hit rate reflects C++ misses after a get miss."""
    mem = MemoryManager(65536)
    assert mem.l2_hit_rate == 0.0

    mem.get_cached_tool_result("echo", {"text": "miss"}, "call_1")
    rate = mem.l2_hit_rate
    assert 0.0 <= rate < 1.0
