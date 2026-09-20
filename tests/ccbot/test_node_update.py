from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ccbot.node_update import GitNodeUpdater, current_git_revision


TARGET = "b" * 40
CURRENT = "a" * 40


def test_current_git_revision_requires_a_clean_tracked_worktree(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command, **_kwargs):
        calls.append(tuple(command))
        if command[3:5] == ["status", "--porcelain"]:
            return SimpleNamespace(stdout=" M src/ccbot/app.py\n", returncode=0)
        return SimpleNamespace(stdout=f"{CURRENT}\n", returncode=0)

    assert current_git_revision(tmp_path, runner=runner) == ""
    assert any("--untracked-files=no" in call for call in calls)


@pytest.mark.asyncio
async def test_git_updater_checks_out_the_exact_fetched_revision(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command, **_kwargs):
        calls.append(tuple(command))
        if command[3:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{CURRENT}\n", returncode=0)
        if command[3:5] == ["status", "--porcelain"]:
            return SimpleNamespace(stdout="", returncode=0)
        if command[3:5] == ["ls-files", "--error-unmatch"]:
            return SimpleNamespace(stdout="", returncode=1)
        return SimpleNamespace(stdout="", returncode=0)

    result = await GitNodeUpdater(tmp_path, runner=runner).update(TARGET)

    assert result == {
        "ok": True,
        "revision": TARGET,
        "previous_revision": CURRENT,
        "restart_required": True,
    }
    assert ("git", "-C", str(tmp_path), "fetch", "--prune", "origin") in calls
    assert (
        "git",
        "-C",
        str(tmp_path),
        "cat-file",
        "-e",
        f"{TARGET}^{{commit}}",
    ) in calls
    assert (
        "git",
        "-C",
        str(tmp_path),
        "checkout",
        "--detach",
        TARGET,
    ) in calls


@pytest.mark.asyncio
async def test_git_updater_refuses_dirty_tracked_worktree(tmp_path: Path) -> None:
    def runner(command, **_kwargs):
        if command[3:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{CURRENT}\n", returncode=0)
        if command[3:5] == ["status", "--porcelain"]:
            return SimpleNamespace(stdout=" M pyproject.toml\n", returncode=0)
        raise AssertionError(f"unexpected command after dirty check: {command}")

    with pytest.raises(RuntimeError, match="tracked changes"):
        await GitNodeUpdater(tmp_path, runner=runner).update(TARGET)


@pytest.mark.asyncio
async def test_git_updater_rolls_back_checkout_when_locked_sync_fails(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command, **_kwargs):
        calls.append(tuple(command))
        if command[0] == "/usr/local/bin/uv":
            return SimpleNamespace(stdout="", stderr="sync failed", returncode=1)
        if command[3:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=f"{CURRENT}\n", returncode=0)
        if command[3:5] == ["status", "--porcelain"]:
            return SimpleNamespace(stdout="", returncode=0)
        if command[3:5] == ["ls-files", "--error-unmatch"]:
            return SimpleNamespace(stdout="uv.lock\n", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(
        "ccbot.node_update.shutil.which", lambda _name: "/usr/local/bin/uv"
    )

    with pytest.raises(RuntimeError, match="dependency sync failed"):
        await GitNodeUpdater(tmp_path, runner=runner).update(TARGET)

    assert calls[-1] == (
        "git",
        "-C",
        str(tmp_path),
        "checkout",
        "--detach",
        CURRENT,
    )


@pytest.mark.asyncio
async def test_git_updater_rejects_non_commit_identifier(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="40-character"):
        await GitNodeUpdater(tmp_path).update("main; rm -rf /")
