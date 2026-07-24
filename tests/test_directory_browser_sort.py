"""Regression test for the session-creation directory picker's sort order.

A directory's own mtime only changes when an entry is added, removed, or
renamed directly inside it — editing a tracked file's content, or a plain
``git commit`` (which only touches objects/refs under ``.git/``), never
touches it. Sorting by the raw ``st_mtime`` alone silently buried
actively-committed git repos under stale scratch directories in the
"most recent first" picker. ``_dir_recency`` additionally checks
``.git/HEAD`` / ``.git/index``.
"""

from __future__ import annotations

import os
import time

from ccbot.handlers.directory_browser import build_directory_browser, _dir_recency


def _touch(path: str, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


class TestDirRecency:
    def test_plain_directory_uses_own_mtime(self, tmp_path) -> None:
        d = tmp_path / "scratch"
        d.mkdir()
        _touch(str(d), 1000.0)
        assert _dir_recency(d) == 1000.0

    def test_git_index_more_recent_than_dir_wins(self, tmp_path) -> None:
        repo = tmp_path / "old-looking-repo"
        git_dir = repo / ".git"
        git_dir.mkdir(parents=True)
        _touch(str(repo), 1000.0)  # stale — no top-level entries touched
        index = git_dir / "index"
        index.write_text("x")
        _touch(str(index), 5000.0)  # a recent `git commit`/`git add`
        assert _dir_recency(repo) == 5000.0

    def test_git_head_checked_too(self, tmp_path) -> None:
        repo = tmp_path / "repo2"
        git_dir = repo / ".git"
        git_dir.mkdir(parents=True)
        _touch(str(repo), 1000.0)
        head = git_dir / "HEAD"
        head.write_text("ref: refs/heads/main")
        _touch(str(head), 4000.0)
        assert _dir_recency(repo) == 4000.0

    def test_missing_git_dir_no_error(self, tmp_path) -> None:
        d = tmp_path / "no-git"
        d.mkdir()
        _touch(str(d), 2000.0)
        assert _dir_recency(d) == 2000.0


class TestBuildDirectoryBrowserOrder:
    def test_actively_committed_repo_sorts_above_stale_scratch_dir(
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

        _, _, subdirs = build_directory_browser(str(tmp_path), user_id=1)
        assert subdirs.index("aaa-old-repo") < subdirs.index("zzz-scratch")
