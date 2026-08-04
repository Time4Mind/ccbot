"""Session-creation directories sort by newest meaningful nested content."""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from ccbot.handlers import directory_browser
from ccbot.handlers.directory_browser import (
    _refresh_recency_tree,
    build_directory_browser,
)


def _touch(path: str, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


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
