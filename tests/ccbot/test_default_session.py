from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import default_session
from ccbot.session import session_manager
from ccbot.session_models import Session
from ccbot.handlers.switcher import session_emoji
from ccbot.bot.callbacks.dir_browser import build_backend_picker


@pytest.fixture(autouse=True)
def _clean_default_state():
    saved_sessions = dict(session_manager.sessions)
    saved_backend = session_manager.agent_backend
    saved_settings = {
        uid: dict(values) for uid, values in session_manager.user_settings.items()
    }
    try:
        session_manager.sessions.clear()
        session_manager.user_settings.clear()
        default_session.reset_default_session_tasks_for_test()
        yield
    finally:
        default_session.reset_default_session_tasks_for_test()
        session_manager.sessions.clear()
        session_manager.sessions.update(saved_sessions)
        session_manager.user_settings.clear()
        session_manager.user_settings.update(saved_settings)
        session_manager.agent_backend = saved_backend


@pytest.mark.asyncio
async def test_enabled_default_session_creates_exactly_one_reserve(
    monkeypatch, tmp_path
):
    user_id = 42
    project = tmp_path / "project"
    project.mkdir()
    session_manager.user_settings[user_id] = {
        "default_session_enabled": True,
        "default_session_directory": str(project),
        "default_session_backend": "codex",
        "enabled_backends": ["codex"],
    }
    create_window = AsyncMock(return_value=(True, "created", "project-3", "@9"))
    monkeypatch.setattr(default_session.tmux_manager, "create_window", create_window)
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    monkeypatch.setattr(session_manager, "mark_window_starting", lambda *_a, **_k: None)

    first, second = await asyncio.gather(
        default_session.ensure_default_session(SimpleNamespace(), user_id),
        default_session.ensure_default_session(SimpleNamespace(), user_id),
    )

    assert first is second
    assert first is not None
    assert first.name == "default"
    assert first.workdir == str(project)
    assert first.backend == "codex"
    assert first.default_reserve_user_id == user_id
    assert session_manager.get_active_session(user_id) is first
    create_window.assert_awaited_once_with(
        str(project), backend="codex", wait_for_codex_ready=True
    )


@pytest.mark.asyncio
async def test_failed_reserve_start_is_bounded_and_does_not_publish_ghost(
    monkeypatch, tmp_path
):
    user_id = 42
    project = tmp_path / "project"
    project.mkdir()
    session_manager.user_settings[user_id] = {
        "default_session_enabled": True,
        "default_session_directory": str(project),
        "default_session_backend": "codex",
        "enabled_backends": ["codex"],
    }
    now = [100.0]
    monkeypatch.setattr(
        default_session,
        "time",
        SimpleNamespace(monotonic=lambda: now[0]),
        raising=False,
    )
    create_window = AsyncMock(return_value=(False, "unsupported prompt", "", ""))
    monkeypatch.setattr(default_session.tmux_manager, "create_window", create_window)

    assert (
        await default_session.ensure_default_session(SimpleNamespace(), user_id) is None
    )
    assert (
        await default_session.ensure_default_session(SimpleNamespace(), user_id) is None
    )
    assert create_window.await_count == 1
    assert session_manager.sessions == {}

    now[0] += 31
    assert (
        await default_session.ensure_default_session(SimpleNamespace(), user_id) is None
    )
    assert create_window.await_count == 2

    now[0] += 30
    assert (
        await default_session.ensure_default_session(SimpleNamespace(), user_id) is None
    )
    assert create_window.await_count == 2

    now[0] += 31
    assert (
        await default_session.ensure_default_session(SimpleNamespace(), user_id) is None
    )
    assert create_window.await_count == 3

    # A changed configuration is a new attempt, not the old failure loop.
    session_manager.user_settings[user_id]["default_session_backend"] = "claude"
    session_manager.user_settings[user_id]["enabled_backends"] = ["claude"]
    assert (
        await default_session.ensure_default_session(SimpleNamespace(), user_id) is None
    )
    assert create_window.await_count == 4


@pytest.mark.asyncio
async def test_first_request_claims_reserve_and_starts_replacement(monkeypatch):
    user_id = 42
    session_manager.user_settings[user_id] = {
        "default_session_enabled": True,
        "default_session_directory": "/tmp/project",
        "default_session_backend": "codex",
        "enabled_backends": ["codex"],
        "haiku_naming": True,
    }
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    sess = session_manager.create_session(
        name="default", window_id="@9", workdir="/tmp/project", backend="codex"
    )
    sess.default_reserve_user_id = user_id
    replacement_started = asyncio.Event()

    async def replacement(_bot, _user_id):
        replacement_started.set()
        return None

    monkeypatch.setattr(default_session, "ensure_default_session", replacement)

    assert default_session.claim_default_session(SimpleNamespace(), user_id, sess)
    await asyncio.wait_for(replacement_started.wait(), timeout=1)

    assert sess.default_reserve_user_id == 0
    assert sess.name == "project-1"


def test_disabled_default_session_creates_nothing(monkeypatch):
    session_manager.user_settings[42] = {"default_session_enabled": False}
    create_window = AsyncMock()
    monkeypatch.setattr(default_session.tmux_manager, "create_window", create_window)

    assert (
        asyncio.run(default_session.ensure_default_session(SimpleNamespace(), 42))
        is None
    )
    create_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabling_default_session_removes_only_empty_reserve(monkeypatch):
    user_id = 42
    session_manager.user_settings[user_id] = {"default_session_enabled": False}
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    monkeypatch.setattr(session_manager, "cancel_window_startup", lambda _wid: None)
    kill_window = AsyncMock(return_value=True)
    monkeypatch.setattr(default_session.tmux_manager, "kill_window", kill_window)
    reserve = session_manager.create_session(name="default", window_id="@R")
    reserve.default_reserve_user_id = user_id
    ordinary = session_manager.create_session(name="project-1", window_id="@1")

    assert (
        await default_session.ensure_default_session(SimpleNamespace(), user_id) is None
    )

    assert session_manager.get_session(reserve.id) is None
    assert session_manager.get_session(ordinary.id) is ordinary
    kill_window.assert_awaited_once_with("@R")


def test_backend_activation_keeps_existing_sessions_and_last_cannot_be_disabled(
    monkeypatch,
):
    user_id = 42
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    session_manager.agent_backend = "codex"
    existing = session_manager.create_session(window_id="@1", backend="codex")

    session_manager.set_backend_enabled(user_id, "claude", True)
    session_manager.set_default_backend(user_id, "claude")
    session_manager.set_backend_enabled(user_id, "codex", False)

    assert existing.backend == "codex"
    assert session_manager.get_enabled_backends(user_id) == ("claude",)
    assert session_manager.get_default_backend(user_id) == "claude"
    with pytest.raises(RuntimeError, match="last backend"):
        session_manager.set_backend_enabled(user_id, "claude", False)


def test_legacy_backend_is_the_only_enabled_backend_by_default(monkeypatch):
    monkeypatch.setattr(session_manager, "agent_backend", "codex")

    assert session_manager.get_enabled_backends(42) == ("codex",)
    assert session_manager.get_default_backend(42) == "codex"


def test_reserve_marker_survives_state_round_trip_and_uses_unique_emoji():
    sess = Session(id="abc", name="default", default_reserve_user_id=42)

    restored = Session.from_dict(sess.to_dict())

    assert restored.default_reserve_user_id == 42
    assert session_emoji(restored) == "⚪"


@pytest.mark.asyncio
async def test_reserve_survives_legacy_round_trip_without_creating_duplicate(
    monkeypatch, tmp_path
):
    user_id = 42
    project = tmp_path / "project"
    project.mkdir()
    session_manager.user_settings[user_id] = {
        "default_session_enabled": True,
        "default_session_directory": str(project),
        "default_session_backend": "codex",
        "enabled_backends": ["codex"],
    }
    original = Session(
        id="reserve",
        name="default",
        window_id="@9",
        workdir=str(project),
        backend="codex",
        default_reserve_user_id=user_id,
    )
    legacy_payload = original.to_dict()
    legacy_payload.pop("default_reserve_user_id")
    restored = Session.from_dict(legacy_payload)
    session_manager.sessions[restored.id] = restored
    create_window = AsyncMock()
    monkeypatch.setattr(default_session.tmux_manager, "create_window", create_window)
    monkeypatch.setattr(session_manager, "save_state", lambda: None)

    reserve = await default_session.ensure_default_session(SimpleNamespace(), user_id)

    assert reserve is restored
    assert restored.default_reserve_user_id == user_id
    assert session_emoji(restored) == "⚪"
    create_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_removes_empty_unmarked_default_duplicate(
    monkeypatch, tmp_path
):
    user_id = 42
    project = tmp_path / "project"
    project.mkdir()
    session_manager.user_settings[user_id] = {
        "default_session_enabled": True,
        "default_session_directory": str(project),
        "default_session_backend": "codex",
        "enabled_backends": ["codex"],
    }
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    monkeypatch.setattr(session_manager, "cancel_window_startup", lambda _wid: None)
    kill_window = AsyncMock(return_value=True)
    create_window = AsyncMock()
    monkeypatch.setattr(default_session.tmux_manager, "kill_window", kill_window)
    monkeypatch.setattr(default_session.tmux_manager, "create_window", create_window)
    orphan = session_manager.create_session(
        name="default", window_id="@old", workdir=str(project), backend="codex"
    )
    reserve = session_manager.create_session(
        name="default",
        window_id="@new",
        workdir=str(project),
        backend="codex",
        default_reserve_user_id=user_id,
    )

    kept = await default_session.ensure_default_session(SimpleNamespace(), user_id)

    assert kept is reserve
    assert session_manager.get_session(orphan.id) is None
    assert session_manager.get_session(reserve.id) is reserve
    kill_window.assert_awaited_once_with("@old")
    create_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_adopts_only_empty_unmarked_default(monkeypatch, tmp_path):
    user_id = 42
    project = tmp_path / "project"
    project.mkdir()
    session_manager.user_settings[user_id] = {
        "default_session_enabled": True,
        "default_session_directory": str(project),
        "default_session_backend": "codex",
        "enabled_backends": ["codex"],
    }
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    create_window = AsyncMock()
    monkeypatch.setattr(default_session.tmux_manager, "create_window", create_window)
    orphan = session_manager.create_session(
        name="default", window_id="@old", workdir=str(project), backend="codex"
    )

    reserve = await default_session.ensure_default_session(SimpleNamespace(), user_id)

    assert reserve is orphan
    assert orphan.default_reserve_owner == user_id
    create_window.assert_not_awaited()


def test_empty_reserve_is_excluded_from_idle_archive(monkeypatch):
    monkeypatch.setattr(session_manager, "save_state", lambda: None)
    sess = session_manager.create_session(window_id="@R")
    sess.default_reserve_user_id = 42
    sess.last_event_at = 1

    assert session_manager.find_idle_to_archive(1) == []


def test_manual_new_session_picker_lists_both_enabled_backends(monkeypatch):
    monkeypatch.setattr(
        session_manager,
        "get_enabled_backends",
        lambda _user_id: ("claude", "codex"),
    )

    keyboard = build_backend_picker(42)

    assert [button.callback_data for button in keyboard.inline_keyboard[0]] == [
        "nb:claude",
        "nb:codex",
    ]
