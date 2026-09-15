"""State-transition contracts for text and rich-media card carriers.

These tests intentionally describe the desired lifecycle at transport boundaries.
They stay separate from the lower-level rich payload tests so carrier state cannot
silently drift when the implementation is reorganized.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.config import config
from ccbot.handlers import (
    card_carrier,
    card_rich_media,
    card_transport,
    card_updates,
    message_sender,
)
from ccbot.handlers.card_binding import bind_carrier, carrier_kind
from ccbot.handlers.card_model import CardState, CarrierKind, Event, TurnPhase


def _assert_text_carrier(state: CardState, *, message_id: int) -> None:
    assert state.msg_id == message_id
    assert carrier_kind(state) is CarrierKind.TEXT
    assert state.is_rich_media_msg is False
    assert state.rich_media_file_id == ""
    assert state.last_pane_hash == ""
    assert state.last_photo_edit_ts == 0.0


@pytest.mark.asyncio
async def test_rich_pane_persists_from_final_into_next_running_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RICH_MEDIA remains the same carrier through IDLE and next RUNNING."""
    text_edit = AsyncMock(return_value=True)
    media_edit = AsyncMock(return_value=True)
    monkeypatch.setattr(config, "rich_messages", True)
    monkeypatch.setattr(message_sender, "try_rich_edit", text_edit)
    monkeypatch.setattr(card_transport, "edit_rich_media_card", media_edit)
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda _uid: True)
    state = CardState(turn_phase=TurnPhase.IDLE)
    bind_carrier(
        state,
        9,
        CarrierKind.RICH_MEDIA,
        rich_media_file_id="pane-file",
        pane_hash="pane-hash",
        photo_edit_ts=10.0,
    )

    assert await card_transport._edit_card_unlocked(
        SimpleNamespace(),
        42,
        state,
        text="final answer",
        reply_markup=SimpleNamespace(),
    )
    assert carrier_kind(state) is CarrierKind.RICH_MEDIA
    assert state.msg_id == 9

    state.turn_phase = TurnPhase.RUNNING
    assert await card_transport._edit_card_unlocked(
        SimpleNamespace(),
        42,
        state,
        text="next turn is running",
        reply_markup=SimpleNamespace(),
    )

    assert media_edit.await_count == 2
    assert state.msg_id == 9
    assert state.is_rich_media_msg is True


@pytest.mark.asyncio
async def test_failed_rich_removal_falls_back_on_same_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected rich edit must still try text fallback before giving up."""
    rich_edit = AsyncMock(return_value=False)
    monkeypatch.setattr(message_sender, "try_rich_edit", rich_edit)
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda _uid: False)
    bot = SimpleNamespace(edit_message_text=AsyncMock(return_value=True))
    state = CardState(turn_phase=TurnPhase.IDLE)
    bind_carrier(
        state,
        9,
        CarrierKind.RICH_MEDIA,
        rich_media_file_id="pane-file",
        pane_hash="pane-hash",
        photo_edit_ts=10.0,
    )

    assert await card_transport._edit_card_unlocked(
        bot, 42, state, text="final answer", reply_markup=SimpleNamespace()
    )

    assert rich_edit.await_count >= 1
    bot.edit_message_text.assert_awaited()
    _assert_text_carrier(state, message_id=9)


@pytest.mark.asyncio
async def test_missing_rich_photo_preserves_existing_carrier_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture/file-id failure is retryable and is not proof of a lost message."""
    monkeypatch.setattr(
        card_rich_media, "lookup_session_for_message", lambda _uid, _mid: None
    )
    state = CardState()
    bind_carrier(state, 9, CarrierKind.RICH_MEDIA)

    assert not await card_rich_media.edit_rich_media_card(
        SimpleNamespace(),
        42,
        state,
        text="running",
        reply_markup=None,
        min_photo_interval=2.5,
    )

    assert state.msg_id == 9
    assert state.is_rich_media_msg is True


@pytest.mark.asyncio
async def test_finalize_waits_for_in_flight_deferred_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not cancel an edit after its Telegram request phase has begun."""
    state = CardState(msg_id=9, pending_edit_in_flight=True)
    release = asyncio.Event()
    was_cancelled = False

    async def in_flight_edit() -> None:
        nonlocal was_cancelled
        try:
            await release.wait()
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        finally:
            state.pending_edit_in_flight = False

    pending = asyncio.create_task(in_flight_edit())
    state.pending_edit = pending

    async def send_final(_bot, _uid, _sess, target, **_kwargs):
        target.msg_id = 10
        return True

    final_send = AsyncMock(side_effect=send_final)
    ensure_seeded = AsyncMock(return_value=None)
    monkeypatch.setattr(card_updates, "get_card_state", lambda _uid, _sess: state)
    monkeypatch.setattr(card_updates, "_should_buffer", lambda *_args: False)
    monkeypatch.setattr(
        card_updates,
        "_legacy",
        lambda name: {
            "_ensure_seeded": ensure_seeded,
            "_render_card": lambda *_args, **_kwargs: "rendered final answer",
            "build_footer_keyboard": lambda *_args, **_kwargs: None,
            "_send_card": final_send,
            "_edit_card": AsyncMock(return_value=True),
        }[name],
    )
    session = SimpleNamespace(id="s1", window_id="")

    finalize = asyncio.create_task(
        card_updates.finalize_task(SimpleNamespace(), 42, session, "final answer")
    )
    await asyncio.sleep(0)
    sent_before_release = final_send.await_count
    release.set()
    await asyncio.gather(pending, finalize, return_exceptions=True)

    assert was_cancelled is False
    assert sent_before_release == 0
    final_send.assert_awaited_once()
    assert state.pending_edit is None


@pytest.mark.asyncio
async def test_active_final_answer_spawns_new_card_and_freezes_open_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = CardState(
        msg_id=9,
        current_page_idx=0,
        events=[Event(type="tool_use", text="old page", started_at=1.0)],
    )
    bot = SimpleNamespace(
        edit_message_reply_markup=AsyncMock(return_value=True),
        delete_message=AsyncMock(return_value=True),
    )
    sent: list[str] = []

    async def send_card(_bot, _uid, _sess, target, *, text, reply_markup=None):
        sent.append(text)
        target.msg_id = 10
        return True

    monkeypatch.setattr(card_updates, "get_card_state", lambda _uid, _sess: state)
    monkeypatch.setattr(card_updates, "_should_buffer", lambda *_args: False)
    monkeypatch.setattr(
        card_updates,
        "_legacy",
        lambda name: {
            "_ensure_seeded": AsyncMock(return_value=None),
            "_render_card": lambda _sess, target, **_kwargs: (
                "✅ active session\nfinal answer"
                if target.completion_marker_pending
                else "active session"
            ),
            "build_footer_keyboard": lambda *_args, **_kwargs: SimpleNamespace(),
            "_send_card": send_card,
            "_edit_card": AsyncMock(return_value=True),
        }[name],
    )
    session = SimpleNamespace(id="s1", window_id="")

    await card_updates.finalize_task(bot, 42, session, "final answer")

    bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=42,
        message_id=9,
        reply_markup=None,
    )
    bot.delete_message.assert_not_awaited()
    assert sent == ["✅ active session\nfinal answer"]
    assert state.msg_id == 10
    assert state.completion_marker_pending is False


@pytest.mark.asyncio
async def test_final_card_is_not_sent_until_render_contains_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale page render must never be accepted as final delivery."""
    state = CardState(
        msg_id=9,
        current_page_idx=0,
        events=[
            Event(
                type="user_msg",
                text="request",
                started_at=1.0,
                is_page_break=True,
            )
        ],
    )
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(return_value=True))
    renders = iter(["stale working page", "request\nfinal answer"])
    sent: list[str] = []

    async def send_card(_bot, _uid, _sess, target, *, text, reply_markup=None):
        sent.append(text)
        target.msg_id = 10
        return True

    monkeypatch.setattr(card_updates, "get_card_state", lambda _uid, _sess: state)
    monkeypatch.setattr(card_updates, "_should_buffer", lambda *_args: False)
    monkeypatch.setattr(
        card_updates,
        "_legacy",
        lambda name: {
            "_ensure_seeded": AsyncMock(return_value=None),
            "_render_card": lambda *_args, **_kwargs: next(renders),
            "build_footer_keyboard": lambda *_args, **_kwargs: SimpleNamespace(),
            "_send_card": send_card,
            "_edit_card": AsyncMock(return_value=True),
        }[name],
    )
    session = SimpleNamespace(id="s1", window_id="")

    await card_updates.finalize_task(bot, 42, session, "final answer")

    assert sent == ["request\nfinal answer"]
    assert state.msg_id == 10


def test_carrier_rebinding_clears_previous_media_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Media flags describe the newly bound message, never an old carrier."""
    key = (42, "to")
    state = CardState()
    bind_carrier(
        state,
        8,
        CarrierKind.RICH_MEDIA,
        rich_media_file_id="pane-file",
        pane_hash="pane-hash",
        photo_edit_ts=10.0,
    )
    card_carrier._cards[key] = state
    monkeypatch.setattr(
        card_carrier.session_manager, "set_card_msg", lambda *_args: None
    )

    try:
        assert card_carrier.transfer_card_to_carrier(42, None, "to", 15) == 8
        _assert_text_carrier(state, message_id=15)
    finally:
        card_carrier._cards.pop(key, None)


def test_switch_uses_target_session_cached_screenshot_not_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = CardState()
    bind_carrier(
        source,
        9,
        CarrierKind.RICH_MEDIA,
        rich_media_file_id="source-photo",
        pane_hash="source-hash",
        photo_edit_ts=50.0,
    )
    target = CardState()
    target_session = SimpleNamespace(
        screenshot_file_id="target-photo",
        screenshot_pane_hash="target-hash",
        screenshot_cached_at=1000.0,
        screenshot_user_id=42,
        screenshot_capture_kib=48,
        screenshot_profile="full8",
    )
    monkeypatch.setattr(
        card_carrier.session_manager,
        "get_session",
        lambda sid: target_session if sid == "to" else None,
    )
    monkeypatch.setattr(
        card_carrier.session_manager, "set_card_msg", lambda *_args: None
    )
    monkeypatch.setattr(card_carrier, "_inline_screens_enabled", lambda _uid: True)
    monkeypatch.setattr(card_carrier.time, "time", lambda: 1005.0)
    monkeypatch.setattr(card_carrier.time, "monotonic", lambda: 75.0)
    card_carrier._cards[(42, "from")] = source
    card_carrier._cards[(42, "to")] = target

    try:
        card_carrier.transfer_card_to_carrier(42, "from", "to", 9)

        assert carrier_kind(target) is CarrierKind.RICH_MEDIA
        assert target.rich_media_file_id == "target-photo"
        assert target.last_pane_hash == "target-hash"
        assert target.last_photo_edit_ts == 75.0
    finally:
        card_carrier._cards.pop((42, "from"), None)
        card_carrier._cards.pop((42, "to"), None)
