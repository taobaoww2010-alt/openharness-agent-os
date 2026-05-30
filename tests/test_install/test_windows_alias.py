"""Installer regressions for Windows command aliases."""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib


def test_pyproject_exposes_daoyi_console_scripts():
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["daoyi"] == "daoyi.cli:app"
    assert scripts["dy"] == "daoyi.cli:app"


def test_powershell_installer_recommends_dy_for_windows():
    script = Path("scripts/install.ps1").read_text(encoding="utf-8")
    assert "dy.exe" in script
    assert "Launch (PowerShell):     dy" in script


def test_powershell_installer_falls_back_when_dy_exe_missing():
    """When `dy.exe` is absent, the installer falls back to `daoyi.exe`."""
    script = Path("scripts/install.ps1").read_text(encoding="utf-8")
    assert "daoyi.exe" in script
    assert "Launch (PowerShell):     dy" in script
