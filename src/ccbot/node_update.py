"""Exact-revision Git updates shared by leader and worker nodes."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

Runner = Callable[..., subprocess.CompletedProcess[str]]
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def runtime_repo_dir() -> Path:
    """Return the checkout containing the running ccbot package."""
    configured = os.environ.get("CCBOT_NODE_REPO_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _invoke(
    runner: Runner, command: list[str]
) -> subprocess.CompletedProcess[str]:
    return runner(command, check=False, text=True, capture_output=True)


def _checked(
    runner: Runner, command: list[str], *, action: str
) -> subprocess.CompletedProcess[str]:
    result = _invoke(runner, command)
    if result.returncode != 0:
        detail = str(getattr(result, "stderr", "")).strip()
        raise RuntimeError(f"{action} failed" + (f": {detail}" if detail else ""))
    return result


def current_git_revision(
    repo_dir: str | Path | None = None,
    *,
    runner: Runner = subprocess.run,
) -> str:
    """Return HEAD only when it describes the running tracked sources exactly."""
    repo = Path(repo_dir).expanduser().resolve() if repo_dir else runtime_repo_dir()
    prefix = ["git", "-C", str(repo)]
    try:
        status = _checked(
            runner,
            [*prefix, "status", "--porcelain", "--untracked-files=no"],
            action="git status",
        )
        if status.stdout.strip():
            return ""
        revision = _checked(
            runner, [*prefix, "rev-parse", "HEAD"], action="git revision"
        ).stdout.strip().lower()
    except (OSError, RuntimeError):
        return ""
    return revision if _COMMIT_RE.fullmatch(revision) else ""


class GitNodeUpdater:
    """Move a clean worker checkout to an exact commit available on its remote."""

    def __init__(
        self,
        repo_dir: str | Path | None = None,
        *,
        remote: str = "origin",
        runner: Runner = subprocess.run,
    ) -> None:
        self.repo_dir = (
            Path(repo_dir).expanduser().resolve() if repo_dir else runtime_repo_dir()
        )
        self.remote = remote
        self._runner = runner

    async def update(self, revision: str) -> dict[str, object]:
        target = revision.strip().lower()
        if not _COMMIT_RE.fullmatch(target):
            raise ValueError("revision must be a 40-character lowercase Git commit SHA")
        return await asyncio.to_thread(self._update_sync, target)

    def _update_sync(self, target: str) -> dict[str, object]:
        prefix = ["git", "-C", str(self.repo_dir)]
        previous = _checked(
            self._runner, [*prefix, "rev-parse", "HEAD"], action="git revision"
        ).stdout.strip().lower()
        if previous == target:
            return {
                "ok": True,
                "revision": target,
                "previous_revision": previous,
                "restart_required": False,
            }
        status = _checked(
            self._runner,
            [*prefix, "status", "--porcelain", "--untracked-files=no"],
            action="git status",
        )
        if status.stdout.strip():
            raise RuntimeError("worker checkout has tracked changes; update refused")
        _checked(
            self._runner,
            [*prefix, "fetch", "--prune", self.remote],
            action="git fetch",
        )
        _checked(
            self._runner,
            [*prefix, "cat-file", "-e", f"{target}^{{commit}}"],
            action="target revision verification",
        )
        _checked(
            self._runner,
            [*prefix, "checkout", "--detach", target],
            action="git checkout",
        )
        try:
            lock = _invoke(
                self._runner,
                [*prefix, "ls-files", "--error-unmatch", "uv.lock"],
            )
            if lock.returncode == 0:
                uv = shutil.which("uv")
                if not uv:
                    raise RuntimeError("tracked uv.lock requires uv on the worker")
                _checked(
                    self._runner,
                    [uv, "sync", "--frozen", "--project", str(self.repo_dir)],
                    action="dependency sync",
                )
        except Exception:
            _invoke(self._runner, [*prefix, "checkout", "--detach", previous])
            raise
        return {
            "ok": True,
            "revision": target,
            "previous_revision": previous,
            "restart_required": True,
        }


__all__ = ["GitNodeUpdater", "current_git_revision", "runtime_repo_dir"]
