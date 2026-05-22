"""Shared fixtures for the ccbot end-to-end suite.

The root ``tests/conftest.py`` already points ``CCBOT_DIR`` at a tmpdir, so
the live ``config`` singleton's state/session-map/monitor-state files are
isolated from the real deployment. These fixtures build on that:

  * ``fake_tmux`` — a fresh :class:`FakeTmuxManager`, patched onto every
    module that imported the ``tmux_manager`` singleton.
  * ``fake_bot`` — a :class:`FakeBot` recorder.
  * ``clean_state`` (autouse) — wipes the module-level ``session_manager``
    maps, the ``notifications`` card caches, the ``bg_status`` per-user map,
    and ``interactive_ui`` trackers before/after each test so tests don't
    bleed into each other (these singletons outlive a single test).
  * ``projects_path`` — a tmpdir wired into ``config.claude_projects_path``
    so JSONL fixtures land where the monitor + session resolver look.

Everything is async-friendly; ``asyncio_mode = auto`` is set in pyproject so
plain ``async def test_*`` functions run without an explicit marker (the
existing suite still decorates with ``@pytest.mark.asyncio`` — we follow that
convention for clarity).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import FakeBot, FakeTmuxManager, USER_ID, install_fake_tmux

__all__ = ["USER_ID"]


@pytest.fixture(autouse=True)
def clean_state():
    """Reset the long-lived module-level singletons around each test."""
    from ccbot.handlers import (
        bg_status,
        interactive_ui,
        notifications,
        status_polling,
    )
    from ccbot.session import session_manager

    def _wipe() -> None:
        session_manager.sessions.clear()
        session_manager.active_sessions.clear()
        session_manager.active_history.clear()
        session_manager.window_states.clear()
        session_manager.window_display_names.clear()
        session_manager.user_window_offsets.clear()
        session_manager.last_switcher_msg_id.clear()
        session_manager.user_settings.clear()
        session_manager.summary_cache.clear()
        notifications._cards.clear()
        notifications._card_locks.clear()
        notifications._repost_intent.clear()
        notifications._msg_to_session.clear()
        bg_status._bg.clear()
        interactive_ui._interactive_msgs.clear()
        interactive_ui._active_interactive_window.clear()
        status_polling._pane_status_cache.clear()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def fake_tmux(monkeypatch) -> FakeTmuxManager:
    fake = FakeTmuxManager()
    install_fake_tmux(monkeypatch, fake)
    return fake


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def projects_path(tmp_path, monkeypatch) -> Path:
    """A tmpdir registered as ``config.claude_projects_path`` so JSONL
    fixtures resolve through the real session/monitor code."""
    from ccbot.config import config

    p = tmp_path / "projects"
    p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "claude_projects_path", p)
    return p


@pytest.fixture
def no_card_lag(monkeypatch):
    """Drop the live-card edit-coalescing lag so a single event renders
    immediately (the default ``live_lag`` is 4s)."""
    from ccbot.session import session_manager

    orig = session_manager.get_user_settings

    def _patched(user_id: int):
        merged = dict(orig(user_id))
        merged["live_lag"] = 0
        return merged

    monkeypatch.setattr(session_manager, "get_user_settings", _patched)
