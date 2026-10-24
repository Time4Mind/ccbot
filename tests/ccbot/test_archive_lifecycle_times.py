"""Archive ordering, retention and displayed age follow distinct lifecycle clocks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import session_state, session_recovery
from ccbot.handlers import archive, switcher
from ccbot.session import SessionManager

DAY = 86400.0


@pytest.fixture
def lifecycle(monkeypatch):
    clock = [20 * DAY]
    monkeypatch.setattr(SessionManager, "_load_state", lambda _self: None)
    manager = SessionManager()
    monkeypatch.setattr(manager, "save_state", lambda: None)
    monkeypatch.setattr(session_state.time, "time", lambda: clock[0])
    monkeypatch.setattr(archive, "session_manager", manager)
    monkeypatch.setattr(switcher, "session_manager", manager)
    monkeypatch.setattr(
        archive, "_archive_blurb", AsyncMock(return_value="description")
    )
    a = manager.create_session(name="A", window_id="@1")
    clock[0] = 23 * DAY
    manager.mark_session_archived(a.id)
    clock[0] = 28 * DAY
    b = manager.create_session(name="B", window_id="@2")
    clock[0] = 29 * DAY
    manager.mark_session_archived(b.id)
    clock[0] = 30 * DAY
    return manager, clock, a, b


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["archive", "lost", "startup", "remote"])
async def test_restored_old_session_returns_first_with_original_age(
    lifecycle, monkeypatch, transition
):
    manager, clock, a, b = lifecycle
    manager.set_session_window(a.id, "@3")
    assert a.created_at == 20 * DAY, "Restoration must preserve creation time"
    if transition == "archive":
        manager.mark_session_archived(a.id)
    elif transition == "lost":
        manager.mark_session_lost(a.id)
    elif transition == "startup":
        monkeypatch.setattr(
            session_recovery.tmux_manager, "list_windows", AsyncMock(return_value=[])
        )
        await session_recovery.reconcile_with_tmux(manager)
    else:
        a.node_id = "worker"
        a.worker_session_id = "routing"
        await session_recovery.reconcile_remote_sessions(
            manager,
            "worker",
            SimpleNamespace(inspect_session=AsyncMock(return_value={"found": False})),
        )
    assert manager.list_archived()[0] is a
    assert a in manager.list_archived(max_age_seconds=2 * DAY)
    assert a not in manager.find_archive_to_purge(2 * DAY)
    body, keyboard = await archive.build_archive_page(
        page=0, lookback_seconds=None, show_all=True, user_id=42
    )
    assert body.index("*A*") < body.index("*B*")
    assert "10d" in body or "10д" in body or "10 д" in body
    if transition != "archive":
        assert a.state == "lost"
        assert "(lost)" in body
    assert a.id in str(keyboard.to_dict())


@pytest.mark.asyncio
async def test_restore_archive_cycles_persist_age_and_new_active_order(
    lifecycle, monkeypatch, tmp_path
):
    from ccbot.config import config
    from ccbot.session import Session

    manager, clock, a, b = lifecycle
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(config, "summary_cache_file", tmp_path / "summaries.json")
    # Use the real persisted session representation between each cycle.
    for step in range(3):
        clock[0] = (30 + step) * DAY
        manager.set_session_window(a.id, f"@{3 + step}")
        live_b = manager.create_session(name=f"newer-{step}", window_id=f"@{10 + step}")
        clock[0] += 10
        manager.set_session_window(a.id, f"@{3 + step}")
        manager.set_active_session(42, a.id)
        keyboard = switcher.build_switcher_keyboard(42)
        ids = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data and button.callback_data.startswith("sw:")
        ]
        assert ids.index("sw:" + live_b.id) < ids.index("sw:" + a.id)
        manager.mark_session_archived(a.id)
        manager.save_state = SessionManager.save_state.__get__(manager)
        manager.save_state()
        import json

        raw = json.loads((tmp_path / "state.json").read_text())
        manager.sessions = {
            sid: Session.from_dict(row) for sid, row in raw["sessions"].items()
        }
        a = manager.get_session(a.id)
        assert a.created_at == 20 * DAY
        assert a.created_at_known is True
        assert manager.list_archived()[0] is a
        body, _ = await archive.build_archive_page(
            page=0, lookback_seconds=None, show_all=True, user_id=42
        )
        assert f"{10 + step}d" in body


@pytest.mark.asyncio
async def test_legacy_creation_is_unknown_until_reliable_provenance(lifecycle):
    from ccbot.session import Session

    manager, _clock, _a, _b = lifecycle
    legacy = Session.from_dict(
        {
            "id": "legacy",
            "name": "Legacy",
            "created_at": 29 * DAY,
            "last_event_at": 29 * DAY,
            "archived_at": 23 * DAY,
            "state": "lost",
        }
    )
    manager.sessions = {legacy.id: legacy}
    body, _ = await archive.build_archive_page(
        page=0, lookback_seconds=None, show_all=True, user_id=42
    )
    assert "*Legacy* _(lost)_ · ?" in body
    assert legacy.created_at_known is False
    manager.set_session_window(legacy.id, "@new")
    assert legacy.created_at == 29 * DAY
    assert Session.from_dict(legacy.to_dict()).created_at_known is False


def test_repeated_remote_misses_do_not_extend_lost_retention(lifecycle):
    manager, clock, a, _b = lifecycle
    manager.set_session_window(a.id, "@3")
    manager.mark_session_lost(a.id)
    first_entry = a.lost_at
    clock[0] += 3 * DAY
    manager.mark_session_lost(a.id)
    assert a.lost_at == first_entry
    assert a not in manager.list_archived(max_age_seconds=2 * DAY)
    assert a in manager.find_archive_to_purge(2 * DAY)
