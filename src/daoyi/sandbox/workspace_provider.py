"""Automatic workspace isolation for tool execution.

Provides git worktree or snapshot-copy based isolation so that
tool operations run in a sandboxed copy of the project directory.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

log = logging.getLogger(__name__)


@dataclass
class WorkspaceHandle:
    """Handle for an isolated workspace. Cleans up on exit."""

    path: Path
    provider: WorkspaceProvider
    _tempdirs: list[Path] = field(default_factory=list)

    def cleanup(self) -> None:
        self.provider.dispose(self)


class WorkspaceProvider(ABC):
    """Abstract base class for workspace isolation providers."""

    id: str
    priority: int

    @abstractmethod
    def is_applicable(self, project_root: Path) -> bool:
        ...

    @abstractmethod
    def prepare(self, project_root: Path) -> WorkspaceHandle:
        ...

    @abstractmethod
    def dispose(self, handle: WorkspaceHandle) -> None:
        ...


class GitWorktreeProvider(WorkspaceProvider):
    """Create a lightweight git worktree for isolation.

    Fast (seconds), shares git objects with the main repo.
    Only applicable for git repositories.
    """

    id = "git-worktree"
    priority = 100
    _worktrees: ClassVar[list[Path]] = []

    def is_applicable(self, project_root: Path) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project_root,
            capture_output=True, text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def prepare(self, project_root: Path) -> WorkspaceHandle:
        slug = _git_output(project_root, "rev-parse", "--short", "HEAD") or "head"
        branch = f"daoyi-workspace-{slug}"
        worktree_root = project_root / ".daoyi" / "worktrees"
        worktree_root.mkdir(parents=True, exist_ok=True)
        worktree_path = worktree_root / slug

        # Remove stale worktree if it exists
        if worktree_path.exists():
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree_path)],
                    cwd=project_root, capture_output=True, timeout=10,
                )
            except Exception:
                shutil.rmtree(worktree_path, ignore_errors=True)

        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"],
            cwd=project_root, capture_output=True, timeout=30, check=True,
        )
        self._worktrees.append(worktree_path)
        log.info("Created git worktree at %s", worktree_path)
        return WorkspaceHandle(path=worktree_path, provider=self)

    def dispose(self, handle: WorkspaceHandle) -> None:
        try:
            from pathlib import Path as _Path
            repo_root = _Path(
                _git_output(handle.path, "rev-parse", "--show-toplevel") or "."
            )
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(handle.path)],
                cwd=repo_root, capture_output=True, timeout=10,
            )
        except Exception as exc:
            log.warning("Failed to remove worktree %s: %s", handle.path, exc)
            shutil.rmtree(handle.path, ignore_errors=True)
        finally:
            if handle.path in self._worktrees:
                self._worktrees.remove(handle.path)


class SnapshotCopyProvider(WorkspaceProvider):
    """Full directory copy for non-git projects.

    Copies the entire project directory to a temp location.
    Slower and more expensive than git worktree, but works everywhere.
    """

    id = "snapshot-copy"
    priority = 50

    def is_applicable(self, project_root: Path) -> bool:
        return project_root.is_dir()

    def prepare(self, project_root: Path) -> WorkspaceHandle:
        tmpdir = Path(tempfile.mkdtemp(prefix="daoyi-workspace-"))
        dst = tmpdir / project_root.name

        # Use rsync for speed (skip .git, node_modules, __pycache__)
        try:
            subprocess.run(
                [
                    "rsync", "-a", "--delete",
                    "--exclude=.git",
                    "--exclude=node_modules",
                    "--exclude=__pycache__",
                    "--exclude=.venv",
                    "--exclude=venv",
                    "--exclude=.daoyi/worktrees",
                    str(project_root) + "/",
                    str(dst) + "/",
                ],
                capture_output=True, timeout=120, check=True,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            # Fallback: shutil copytree
            shutil.copytree(
                project_root, dst,
                ignore=shutil.ignore_patterns(
                    ".git", "node_modules", "__pycache__", ".venv", "venv",
                    ".daoyi/worktrees",
                ),
                dirs_exist_ok=True,
            )

        log.info("Created snapshot copy at %s", dst)
        return WorkspaceHandle(path=dst, provider=self, _tempdirs=[tmpdir])

    def dispose(self, handle: WorkspaceHandle) -> None:
        for td in handle._tempdirs:
            shutil.rmtree(td, ignore_errors=True)


def get_workspace_provider(project_root: Path) -> WorkspaceProvider | None:
    """Select the best workspace provider for the given project root."""
    providers: list[WorkspaceProvider] = [
        GitWorktreeProvider(),
        SnapshotCopyProvider(),
    ]
    providers.sort(key=lambda p: p.priority, reverse=True)
    for p in providers:
        if p.is_applicable(project_root):
            return p
    return None


def _git_output(cwd: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return (result.stdout or "").strip() if result.returncode == 0 else None
