# Licensed under the Apache License, Version 2.0.
"""OpenHarness Agent OS — kernel subsystem."""

from __future__ import annotations

import sys
from pathlib import Path

build_path = Path(__file__).resolve().parent.parent.parent / "cpp-core" / "build" / "bindings" / "python"
if build_path.exists() and str(build_path) not in sys.path:
    sys.path.insert(0, str(build_path))

try:
    import _daoyi as _CPP
    HAS_CPP_CORE = True
except ImportError:
    HAS_CPP_CORE = False

__all__ = ["HAS_CPP_CORE"]
