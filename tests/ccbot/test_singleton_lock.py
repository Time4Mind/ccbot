"""Tests for the singleton flock that prevents two bot instances from
both calling Telegram's exclusive ``getUpdates``."""

from __future__ import annotations

import fcntl
from pathlib import Path

import pytest

from ccbot.main import _acquire_singleton_lock


def test_fresh_path_locks_and_returns_handle(tmp_path: Path) -> None:
    lock = tmp_path / "ccbot.lock"
    fh = _acquire_singleton_lock(lock)
    try:
        assert lock.exists()
        assert not fh.closed
        # FD_CLOEXEC is set so the lock doesn't leak into subprocess children.
        flags = fcntl.fcntl(fh.fileno(), fcntl.F_GETFD)
        assert flags & fcntl.FD_CLOEXEC
    finally:
        fh.close()


def test_second_acquirer_exits(tmp_path: Path) -> None:
    lock = tmp_path / "ccbot.lock"
    held = _acquire_singleton_lock(lock)
    try:
        # The first call held the lock; the second must hit sys.exit(1)
        # because LOCK_NB returns OSError immediately when contended.
        with pytest.raises(SystemExit) as exc:
            _acquire_singleton_lock(lock)
        assert exc.value.code == 1
    finally:
        held.close()


def test_released_lock_can_be_reacquired(tmp_path: Path) -> None:
    # Holder dies → fcntl releases the lock automatically when the fd
    # closes. A fresh start (next supervisor cycle, etc.) should be
    # able to come up cleanly without a "stale lock file" sweep.
    lock = tmp_path / "ccbot.lock"
    first = _acquire_singleton_lock(lock)
    first.close()
    second = _acquire_singleton_lock(lock)
    try:
        assert not second.closed
    finally:
        second.close()


def test_creates_parent_directory(tmp_path: Path) -> None:
    # CCBOT_DIR may not exist on first launch; the lock acquire shouldn't
    # crash with FileNotFoundError before the rest of bootstrap creates
    # state.json.
    nested = tmp_path / "fresh" / "subdir" / "ccbot.lock"
    fh = _acquire_singleton_lock(nested)
    try:
        assert nested.exists()
    finally:
        fh.close()
