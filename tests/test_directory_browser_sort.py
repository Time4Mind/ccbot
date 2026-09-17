"""Session-creation directories sort by newest meaningful nested content."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccbot.handlers import directory_browser
from ccbot.handlers.directory_browser import (
    DIRS_PER_PAGE,
    _refresh_recency_tree,
    build_directory_browser,
)


def _touch(path: str, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


class _FakeEntry:
    def __init__(self, name: str, path: str, *, is_dir: bool, mtime: float) -> None:
        self.name = name
        self.path = path
        self._is_dir = is_dir
        self._mtime = mtime

    def is_dir(self, *, follow_symlinks: bool = False) -> bool:
        return self._is_dir

    def stat(self, *, follow_symlinks: bool = False):
        return SimpleNamespace(st_mtime=self._mtime)


class _FakeScandir:
    def __init__(self, entries, *, error_after: int | None = None) -> None:
        self._entries = iter(entries)
        self._seen = 0
        self._error_after = error_after

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def __iter__(self):
        return self

    def __next__(self):
        if self._error_after is not None and self._seen >= self._error_after:
            raise OSError(22, "volatile directory entry disappeared")
        self._seen += 1
        return next(self._entries)


async def _wait_for_refreshes() -> None:
    tasks = list(directory_browser._RECENCY_REFRESH_TASKS.values())
    if tasks:
        await asyncio.gather(*tasks)


class TestDirRecency:
    @pytest.fixture(autouse=True)
    def clear_cache(self):
        directory_browser._RECENCY_CACHE.clear()
        directory_browser._RECENCY_REFRESH_TASKS.clear()
        yield
        directory_browser._RECENCY_CACHE.clear()
        directory_browser._RECENCY_REFRESH_TASKS.clear()

    def test_plain_directory_uses_own_mtime(self, tmp_path) -> None:
        d = tmp_path / "scratch"
        d.mkdir()
        _touch(str(d), 1000.0)
        assert _refresh_recency_tree(d) == 1000.0

    def test_git_index_more_recent_than_dir_wins(self, tmp_path) -> None:
        repo = tmp_path / "old-looking-repo"
        git_dir = repo / ".git"
        git_dir.mkdir(parents=True)
        _touch(str(repo), 1000.0)  # stale — no top-level entries touched
        index = git_dir / "index"
        index.write_text("x")
        _touch(str(index), 5000.0)  # a recent `git commit`/`git add`
        assert _refresh_recency_tree(repo) == 5000.0

    def test_git_head_checked_too(self, tmp_path) -> None:
        repo = tmp_path / "repo2"
        git_dir = repo / ".git"
        git_dir.mkdir(parents=True)
        _touch(str(repo), 1000.0)
        head = git_dir / "HEAD"
        head.write_text("ref: refs/heads/main")
        _touch(str(head), 4000.0)
        assert _refresh_recency_tree(repo) == 4000.0

    def test_missing_git_dir_no_error(self, tmp_path) -> None:
        d = tmp_path / "no-git"
        d.mkdir()
        _touch(str(d), 2000.0)
        assert _refresh_recency_tree(d) == 2000.0

    def test_nested_file_mtime_wins(self, tmp_path) -> None:
        project = tmp_path / "project"
        nested = project / "src" / "package"
        nested.mkdir(parents=True)
        source = nested / "feature.py"
        source.write_text("print('new')")
        for path in (project, project / "src", nested):
            _touch(str(path), 1000.0)
        _touch(str(source), 7000.0)

        assert _refresh_recency_tree(project) == 7000.0

    def test_generated_dependency_tree_does_not_win(self, tmp_path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        source = project / "app.py"
        source.write_text("old")
        dependency = project / ".venv" / "lib" / "package.py"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("generated")
        _touch(str(project), 1000.0)
        _touch(str(source), 3000.0)
        _touch(str(dependency), 9000.0)

        assert _refresh_recency_tree(project) == 3000.0

    def test_configured_directory_does_not_affect_recency(
        self, tmp_path, monkeypatch
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        source = project / "app.py"
        source.write_text("old")
        excluded = project / ".private-index"
        excluded.mkdir()
        generated = excluded / "latest-state"
        generated.write_text("generated")
        _touch(str(project), 1000.0)
        _touch(str(source), 3000.0)
        _touch(str(excluded), 4000.0)
        _touch(str(generated), 9000.0)
        monkeypatch.setattr(
            directory_browser.config,
            "recency_exclude_dirs",
            frozenset({".private-index"}),
        )

        assert _refresh_recency_tree(project) == 3000.0

    def test_linux_root_keeps_virtual_dirs_shallow(self, tmp_path, monkeypatch) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        source = workspace / "recent.py"
        source.write_text("recent")
        _touch(str(workspace), 1000.0)
        _touch(str(source), 8000.0)

        real_scandir = os.scandir
        real_path_stat = Path.stat
        scanned: list[Path] = []
        virtual_mtimes = {
            Path("/proc"): 7001.0,
            Path("/sys"): 7002.0,
            Path("/dev"): 7003.0,
            Path("/run"): 7004.0,
        }

        def fake_scandir(path):
            current = Path(path)
            scanned.append(current)
            if current == Path("/"):
                return _FakeScandir(
                    [
                        _FakeEntry(
                            virtual.name,
                            str(virtual),
                            is_dir=True,
                            mtime=mtime,
                        )
                        for virtual, mtime in virtual_mtimes.items()
                    ]
                    + [
                        _FakeEntry(
                            "workspace",
                            str(workspace),
                            is_dir=True,
                            mtime=1000.0,
                        ),
                    ]
                )
            if current in virtual_mtimes:
                raise AssertionError(f"Linux root indexing recursed into {current}")
            return real_scandir(path)

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(directory_browser.os, "scandir", fake_scandir)
        monkeypatch.setattr(
            Path,
            "stat",
            lambda path, *args, **kwargs: (
                SimpleNamespace(st_mtime=virtual_mtimes[path])
                if path in virtual_mtimes
                else real_path_stat(path, *args, **kwargs)
            ),
        )

        _refresh_recency_tree(Path("/"))

        assert not virtual_mtimes.keys() & scanned
        for virtual, mtime in virtual_mtimes.items():
            assert directory_browser._RECENCY_CACHE[str(virtual)][1] == mtime
        assert directory_browser._RECENCY_CACHE[str(workspace)][1] == 8000.0

    def test_nested_project_directory_named_proc_is_still_scanned(
        self, tmp_path, monkeypatch
    ) -> None:
        project = tmp_path / "project"
        nested = project / "proc"
        nested.mkdir(parents=True)
        source = nested / "worker.py"
        source.write_text("recent")
        _touch(str(project), 1000.0)
        _touch(str(nested), 1000.0)
        _touch(str(source), 9000.0)
        monkeypatch.setattr(sys, "platform", "linux")

        assert _refresh_recency_tree(project) == 9000.0
        assert directory_browser._RECENCY_CACHE[str(nested)][1] == 9000.0

    def test_iteration_error_in_one_subtree_does_not_abort_siblings(
        self, tmp_path, monkeypatch
    ) -> None:
        volatile = tmp_path / "a-volatile"
        volatile.mkdir()
        sibling = tmp_path / "z-sibling"
        sibling.mkdir()
        recent = sibling / "recent.py"
        recent.write_text("recent")
        _touch(str(tmp_path), 1000.0)
        _touch(str(volatile), 1000.0)
        _touch(str(sibling), 1000.0)
        _touch(str(recent), 8000.0)

        real_scandir = os.scandir

        def fake_scandir(path):
            if Path(path) == volatile:
                return _FakeScandir(
                    [
                        _FakeEntry(
                            "vanishing",
                            str(volatile / "vanishing"),
                            is_dir=False,
                            mtime=5000.0,
                        )
                    ],
                    error_after=1,
                )
            return real_scandir(path)

        monkeypatch.setattr(directory_browser.os, "scandir", fake_scandir)

        assert _refresh_recency_tree(tmp_path) == 8000.0
        assert directory_browser._RECENCY_CACHE[str(sibling)][1] == 8000.0

    @pytest.mark.parametrize("platform", ["darwin", "win32"])
    def test_non_linux_root_keeps_existing_recursive_behavior(
        self, monkeypatch, platform
    ) -> None:
        real_scandir = os.scandir
        real_path_stat = Path.stat
        scanned: list[Path] = []

        def fake_scandir(path):
            current = Path(path)
            scanned.append(current)
            if current == Path("/"):
                return _FakeScandir(
                    [_FakeEntry("proc", "/proc", is_dir=True, mtime=1000.0)]
                )
            if current == Path("/proc"):
                return _FakeScandir([])
            return real_scandir(path)

        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(directory_browser.os, "scandir", fake_scandir)
        monkeypatch.setattr(
            Path,
            "stat",
            lambda path, *args, **kwargs: (
                SimpleNamespace(st_mtime=1000.0)
                if path == Path("/proc")
                else real_path_stat(path, *args, **kwargs)
            ),
        )

        _refresh_recency_tree(Path("/"))

        assert Path("/proc") in scanned


class TestBuildDirectoryBrowserOrder:
    @pytest.fixture(autouse=True)
    def clear_cache(self):
        directory_browser._RECENCY_CACHE.clear()
        directory_browser._RECENCY_REFRESH_TASKS.clear()
        yield
        directory_browser._RECENCY_CACHE.clear()
        directory_browser._RECENCY_REFRESH_TASKS.clear()

    @pytest.mark.asyncio
    async def test_actively_committed_repo_sorts_above_stale_scratch_dir(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "ccbot.handlers.directory_browser.config.show_hidden_dirs", False
        )
        now = time.time()

        # A git repo whose top-level dir hasn't been touched in ages, but
        # was committed to recently (the exact real-world case this fixes).
        repo = tmp_path / "aaa-old-repo"
        (repo / ".git").mkdir(parents=True)
        _touch(str(repo), now - 90 * 86400)
        index = repo / ".git" / "index"
        index.write_text("x")
        _touch(str(index), now - 3600)  # committed 1h ago

        # A scratch dir whose own mtime is more recent than the repo's
        # top-level dir, but older than the repo's real last-touch time.
        scratch = tmp_path / "zzz-scratch"
        scratch.mkdir()
        _touch(str(scratch), now - 86400)  # touched 1 day ago

        _, _, subdirs = await build_directory_browser(str(tmp_path), user_id=1)
        await _wait_for_refreshes()
        assert subdirs.index("aaa-old-repo") < subdirs.index("zzz-scratch")

    @pytest.mark.asyncio
    async def test_nested_content_sorts_container_first(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "ccbot.handlers.directory_browser.config.show_hidden_dirs", False
        )
        active = tmp_path / "aaa-container"
        nested = active / "project" / "src"
        nested.mkdir(parents=True)
        changed = nested / "changed.py"
        changed.write_text("latest")
        stale = tmp_path / "zzz-directly-touched"
        stale.mkdir()

        for path in (active, active / "project", nested):
            _touch(str(path), 1000.0)
        _touch(str(changed), 5000.0)
        _touch(str(stale), 3000.0)

        # First paint is metadata-only and schedules one background pass.
        await build_directory_browser(str(tmp_path), user_id=1)
        await _wait_for_refreshes()
        _, _, subdirs = await build_directory_browser(str(tmp_path), user_id=1)

        assert subdirs.index("aaa-container") < subdirs.index("zzz-directly-touched")

    @pytest.mark.asyncio
    async def test_background_pass_populates_nested_directory_cache(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "ccbot.handlers.directory_browser.config.show_hidden_dirs", False
        )
        nested = tmp_path / "container" / "project" / "src"
        nested.mkdir(parents=True)
        changed = nested / "changed.py"
        changed.write_text("latest")
        for path in (
            tmp_path / "container",
            tmp_path / "container" / "project",
            nested,
        ):
            _touch(str(path), 1000.0)
        _touch(str(changed), 8000.0)

        await build_directory_browser(str(tmp_path), user_id=1)
        await _wait_for_refreshes()

        assert directory_browser._RECENCY_CACHE[str(nested)][1] == 8000.0

    @pytest.mark.asyncio
    async def test_pagination_cycles_and_create_folder_is_visible(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "ccbot.handlers.directory_browser.config.show_hidden_dirs", False
        )
        for idx in range(DIRS_PER_PAGE + 1):
            (tmp_path / f"dir-{idx}").mkdir()

        _text, keyboard, _subdirs = await build_directory_browser(
            str(tmp_path), page=0, user_id=1
        )
        rows = keyboard.inline_keyboard
        pager = next(row for row in rows if any(button.text == "1/2" for button in row))
        assert [button.text for button in pager] == ["◀", "1/2", "▶"]
        assert [button.callback_data for button in pager] == [
            "db:page:1",
            "db:page:0",
            "db:page:1",
        ]
        assert any(
            button.callback_data == "db:create" for row in rows for button in row
        )
