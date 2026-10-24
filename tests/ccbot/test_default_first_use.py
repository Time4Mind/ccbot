"""A prewarmed reserve starts its logical lifetime at first accepted input."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import default_session
from ccbot.bot import inbound
from ccbot.handlers import archive, switcher
from ccbot.handlers.card_types import CardState
from ccbot.inbound_queue import shutdown_inbound_queues
from ccbot.session import Session, session_manager


def buttons():
    keyboard = switcher.build_switcher_keyboard(42, include_new=False)
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


@pytest.fixture
async def first_use(monkeypatch):
    clock = [400.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    reserve = Session(
        id="reserve",
        name="default",
        window_id="@R",
        created_at=100,
        activated_at=100,
        last_event_at=100,
        default_reserve_user_id=42,
        workdir="/tmp/project",
    )
    a = Session(id="a", name="A", window_id="@A", created_at=200, activated_at=200)
    b = Session(id="b", name="B", window_id="@B", created_at=300, activated_at=300)
    monkeypatch.setattr(session_manager, "sessions", {s.id: s for s in (reserve, a, b)})
    monkeypatch.setattr(session_manager, "active_sessions", {42: reserve.id})
    monkeypatch.setattr(session_manager, "active_sessions_by_node", {})
    monkeypatch.setattr(session_manager, "active_history", {})
    monkeypatch.setattr(session_manager, "user_settings", {})
    monkeypatch.setattr(session_manager, "save_state", lambda: None)

    async def replacement(_bot, _uid):
        new = Session(
            id="replacement",
            name="default",
            window_id="@new",
            created_at=clock[0],
            activated_at=clock[0],
            default_reserve_user_id=42,
        )
        session_manager.sessions[new.id] = new
        return new

    monkeypatch.setattr(default_session, "ensure_default_session", replacement)
    yield reserve, clock
    await shutdown_inbound_queues()
    await default_session.shutdown_default_session_tasks()


@pytest.mark.asyncio
async def test_first_request_sets_chronology_and_age_once(first_use, monkeypatch):
    reserve, clock = first_use
    assert buttons() == ["sw:a", "sw:b", "sw:reserve"]
    assert default_session.claim_default_session(SimpleNamespace(), 42, reserve)
    await asyncio.sleep(0)
    assert buttons() == ["sw:a", "sw:b", "sw:reserve", "sw:replacement"]
    assert reserve.created_at == reserve.activated_at == 400
    assert reserve.created_at_known is True
    clock[0] = 500
    assert not default_session.claim_default_session(SimpleNamespace(), 42, reserve)
    assert reserve.created_at == reserve.activated_at == 400
    later = Session(
        id="later", name="Later", window_id="@L", created_at=500, activated_at=500
    )
    session_manager.sessions[later.id] = later
    assert buttons() == ["sw:a", "sw:b", "sw:reserve", "sw:later", "sw:replacement"]
    # Round-trip the persisted representation, then render the actual archive.
    session_manager.sessions[reserve.id] = Session.from_dict(reserve.to_dict())
    reserve = session_manager.sessions[reserve.id]
    session_manager.set_session_window(reserve.id, "@restored")
    assert reserve.created_at == 400 and reserve.activated_at == 500
    session_manager.mark_session_archived(reserve.id)
    monkeypatch.setattr(
        archive, "_archive_blurb", AsyncMock(return_value="description")
    )
    clock[0] = 4600
    body, _keyboard = await archive.build_archive_page(
        page=0, lookback_seconds=None, show_all=True, user_id=42
    )
    assert "*project 1* · 1h" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "voice", "photo", "document"])
async def test_content_admission_precedes_slow_processing_and_burst(
    first_use, monkeypatch, kind
):
    reserve, clock = first_use
    started = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def processor(_update, _context, wid):
        seen.append((wid, reserve.created_at))
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(inbound, "is_user_allowed", lambda _: True)
    monkeypatch.setattr(inbound, "_capture_pending_transfer", lambda *_: False)
    monkeypatch.setattr(inbound, "get_flow", lambda _: None)
    monkeypatch.setattr(inbound, "get_card_state", lambda *_: CardState())
    monkeypatch.setattr(inbound, "schedule_card_after_message", lambda *_a, **_kw: None)
    monkeypatch.setattr(inbound, "refresh_session_keyboard", AsyncMock())
    monkeypatch.setattr(inbound, "_run_" + kind, processor)
    context = SimpleNamespace(bot=SimpleNamespace(), user_data={})
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(
            message_id=1,
            text="request" if kind == "text" else None,
            caption=None,
            reply_to_message=None,
        ),
    )
    handler = getattr(inbound, kind + "_intake_handler")
    assert await handler(update, context)
    assert reserve.created_at == reserve.activated_at == 400
    await asyncio.wait_for(started.wait(), 1)
    clock[0] = 500
    update2 = SimpleNamespace(
        effective_user=update.effective_user,
        message=SimpleNamespace(
            message_id=2,
            text="follow-up" if kind == "text" else None,
            caption=None,
            reply_to_message=None,
        ),
    )
    assert await handler(update2, context)
    assert reserve.created_at == reserve.activated_at == 400
    # Selection changes while transcription/preprocessing is blocked do not
    # change which session owns either admitted request.
    session_manager.active_sessions[42] = "a"
    clock[0] = 700
    release.set()
    from ccbot.inbound_queue import has_pending_inbound

    while has_pending_inbound("@R"):
        await asyncio.sleep(0)
    assert seen == [("@R", 400), ("@R", 400)]
    assert buttons() == ["sw:a", "sw:b", "sw:reserve", "sw:replacement"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["command", "unsupported"])
async def test_control_input_does_not_start_reserve_lifetime(
    first_use, monkeypatch, kind
):
    reserve, _clock = first_use
    monkeypatch.setattr(inbound, "is_user_allowed", lambda _: True)
    monkeypatch.setattr(inbound, "get_card_state", lambda *_: CardState())
    monkeypatch.setattr(inbound, "schedule_card_after_message", lambda *_a, **_kw: None)
    context = SimpleNamespace(bot=SimpleNamespace())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(message_id=1, text=".", caption=None),
    )
    assert inbound._enqueue(
        update, context, kind=kind, processor=AsyncMock(return_value=True)
    )
    assert reserve.default_reserve_user_id == 42
    assert reserve.created_at == reserve.activated_at == 100
    assert buttons() == ["sw:a", "sw:b", "sw:reserve"]


@pytest.mark.asyncio
async def test_legacy_unused_reserve_gains_reliable_creation_at_first_use(first_use):
    reserve, _clock = first_use
    reserve.created_at_known = False
    assert default_session.claim_default_session(SimpleNamespace(), 42, reserve)
    assert reserve.created_at_known is True and reserve.created_at == 400


@pytest.mark.asyncio
async def test_already_used_session_history_is_untouched(first_use):
    reserve, _clock = first_use
    reserve.clear_default_reserve()
    reserve.created_at_known = False
    assert not default_session.claim_default_session(SimpleNamespace(), 42, reserve)
    assert reserve.created_at == reserve.activated_at == 100
    assert reserve.created_at_known is False


@pytest.mark.asyncio
async def test_first_use_survives_real_state_save_load(
    first_use, monkeypatch, tmp_path
):
    from ccbot.config import config
    from ccbot.session import SessionManager
    from ccbot.handlers import bg_status

    reserve, clock = first_use
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(config, "summary_cache_file", tmp_path / "summaries.json")
    monkeypatch.setattr(bg_status, "_bg", {})
    assert default_session.claim_default_session(SimpleNamespace(), 42, reserve)
    await asyncio.sleep(0)
    SessionManager.save_state(session_manager)
    reloaded = SessionManager()
    restored = reloaded.get_session(reserve.id)
    assert restored.created_at == restored.activated_at == 400
    assert restored.created_at_known is True
    clock[0] = 500
    reloaded.set_session_window(restored.id, "@restored")
    assert restored.created_at == 400 and restored.activated_at == 500
