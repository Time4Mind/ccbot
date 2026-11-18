"""Persist the cause of the latest archival through the normal entry points."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import session_state
from ccbot.bot.callbacks import confirm
from ccbot.bot.commands import lifecycle
from ccbot.config import config
from ccbot.handlers import archive, notifications
from ccbot.handlers.callback_data import CB_CONF_KILL_NO, CB_CONF_KILL_YES
from ccbot.session import SessionManager

USER_ID = 42
HOUR = 3600


@pytest.fixture
def storage(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(config, "state_file", state_file)
    monkeypatch.setattr(config, "summary_cache_file", tmp_path / "summaries.json")
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Synthetic context"},
            }
        )
        + "\n"
    )
    monkeypatch.setattr(archive, "build_session_file_path", lambda *_: transcript)
    for module in (archive, lifecycle):
        monkeypatch.setattr(module, "teardown_session_runtime", AsyncMock())
    monkeypatch.setattr(confirm, "open_more_in_place", AsyncMock())
    monkeypatch.setattr(confirm, "open_sessions_in_place", AsyncMock())
    monkeypatch.setattr(notifications, "refresh_session_keyboard", AsyncMock())
    clock = [10 * HOUR]
    monkeypatch.setattr(session_state.time, "time", lambda: clock[0])

    def reload():
        manager = SessionManager()
        manager.user_settings[USER_ID] = {"session_idle_hours": 6}
        for module in (archive, lifecycle, confirm):
            monkeypatch.setattr(module, "session_manager", manager)
        return manager

    return state_file, clock, reload


async def confirm_close(session_id, *, yes=True):
    query = SimpleNamespace(
        data=f"{CB_CONF_KILL_YES}{session_id}" if yes else CB_CONF_KILL_NO,
        answer=AsyncMock(),
    )
    assert await confirm.handle(
        query, SimpleNamespace(bot=object()), SimpleNamespace(id=USER_ID)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_first", [True, False])
async def test_rearchive_records_latest_cause_across_restart(storage, manual_first):
    state_file, clock, reload = storage
    manager = reload()
    sess = manager.create_session(
        name="populated", window_id="@11", workdir="/tmp", backend="codex"
    )
    manager.set_session_claude_id(sess.id, "has-transcript")
    sid = sess.id

    async def close(manual):
        clock[0] += 7 * HOUR
        if manual:
            await confirm_close(sid)
        else:
            assert await archive.idle_archive_sweep(object(), USER_ID) == 1

    await close(manual_first)
    manager = reload()
    first = manager.sessions[sid]
    first_stamp = first.archived_at
    assert first.state == "archived"
    assert first.archived_manually is manual_first
    first_record = json.loads(state_file.read_text())["sessions"][sid]
    assert first_record["archived_manually"] is manual_first
    assert first_record["archived_at"] == clock[0]

    clock[0] += HOUR
    manager.set_session_window(sid, "@12")  # Successful restore publishes this binding.
    restored = reload().sessions[sid]
    assert restored.state == "active"
    assert restored.archived_at == first_stamp
    assert restored.archived_manually is manual_first

    await close(not manual_first)
    latest = reload().sessions[sid]
    assert latest.state == "archived"
    assert latest.archived_at > first_stamp
    assert latest.archived_manually is not manual_first
    current = state_file.read_bytes()
    await confirm_close(sid)  # Duplicate confirmation against an archived record.
    assert state_file.read_bytes() == current


def test_legacy_archive_reason_is_unknown(storage):
    state_file, _, reload = storage
    state_file.write_text(
        json.dumps(
            {
                "sessions": {
                    "legacy": {"id": "legacy", "state": "archived", "archived_at": 1}
                }
            }
        )
    )
    assert reload().sessions["legacy"].archived_manually is None


@pytest.mark.asyncio
async def test_cancelled_close_and_empty_session_do_not_create_archive(storage):
    state_file, _, reload = storage
    manager = reload()
    sess = manager.create_session(name="empty", window_id="@11")
    before = state_file.read_bytes()
    await confirm_close(sess.id, yes=False)
    assert state_file.read_bytes() == before
    assert reload().sessions[sess.id].state == "active"

    await confirm_close(sess.id)
    assert sess.id not in reload().sessions
    assert sess.id not in json.loads(state_file.read_text())["sessions"]
