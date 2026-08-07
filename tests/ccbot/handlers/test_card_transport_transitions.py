"""State-transition contracts for text, rich-media, and legacy card carriers.

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
from ccbot.handlers.card_model import CardState, CarrierKind, TurnPhase


def _assert_text_carrier(state: CardState, *, message_id: int) -> None:
    assert state.msg_id == message_id
    assert carrier_kind(state) is CarrierKind.TEXT
    assert state.is_rich_media_msg is False
    assert state.rich_media_file_id == ""
    assert state.is_photo_msg is False
    assert state.last_pane_hash == ""
    assert state.last_photo_edit_ts == 0.0


def _wire_legacy_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        card_transport, "lookup_session_for_message", lambda _uid, _mid: "s1"
    )
    monkeypatch.setattr(card_transport, "_register_msg", lambda *_args: None)
    monkeypatch.setattr(card_transport, "_strip_stale_switchers", AsyncMock())
    monkeypatch.setattr(
        card_transport.session_manager, "set_card_msg", lambda *_args: None
    )
    monkeypatch.setattr(
        card_transport.session_manager, "set_last_switcher_msg", lambda *_args: None
    )


@pytest.mark.asyncio
async def test_running_turn_restores_rich_pane_after_final_text_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RICH_MEDIA -> final TEXT -> next running turn -> RICH_MEDIA again."""
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
    _assert_text_carrier(state, message_id=9)

    state.turn_phase = TurnPhase.RUNNING
    assert await card_transport._edit_card_unlocked(
        SimpleNamespace(),
        42,
        state,
        text="next turn is running",
        reply_markup=SimpleNamespace(),
    )

    media_edit.assert_awaited_once()
    assert state.msg_id == 9
    assert state.is_rich_media_msg is True
    assert state.is_photo_msg is False


@pytest.mark.asyncio
async def test_failed_rich_removal_falls_back_on_same_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected rich edit must still try text fallback before giving up."""
    rich_edit = AsyncMock(return_value=False)
    monkeypatch.setattr(message_sender, "try_rich_edit", rich_edit)
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
async def test_final_legacy_photo_is_replaced_send_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy photo cannot shed media in place; replace it without data loss."""
    _wire_legacy_replacement(monkeypatch)
    order: list[str] = []

    async def send_text(*_args: object, **_kwargs: object) -> SimpleNamespace:
        order.append("send")
        return SimpleNamespace(message_id=12)

    async def delete_message(**_kwargs: object) -> bool:
        order.append("delete")
        return True

    send = AsyncMock(side_effect=send_text)
    monkeypatch.setattr(message_sender, "send_with_fallback", send)
    bot = SimpleNamespace(delete_message=AsyncMock(side_effect=delete_message))
    state = CardState(turn_phase=TurnPhase.IDLE)
    bind_carrier(
        state,
        9,
        CarrierKind.LEGACY_PHOTO,
        pane_hash="pane-hash",
        photo_edit_ts=10.0,
    )

    assert await card_transport._edit_card_unlocked(
        bot, 42, state, text="final answer", reply_markup=SimpleNamespace()
    )

    assert order == ["send", "delete"]
    bot.delete_message.assert_awaited_once_with(chat_id=42, message_id=9)
    _assert_text_carrier(state, message_id=12)


@pytest.mark.asyncio
async def test_failed_legacy_photo_replacement_rolls_back_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the old photo carrier bound when both replacement sends fail."""
    _wire_legacy_replacement(monkeypatch)
    send = AsyncMock(return_value=None)
    monkeypatch.setattr(message_sender, "send_with_fallback", send)
    bot = SimpleNamespace(delete_message=AsyncMock(return_value=True))
    state = CardState(turn_phase=TurnPhase.IDLE)
    bind_carrier(
        state,
        9,
        CarrierKind.LEGACY_PHOTO,
        pane_hash="pane-hash",
        photo_edit_ts=10.0,
    )

    assert not await card_transport._edit_card_unlocked(
        bot, 42, state, text="final answer", reply_markup=SimpleNamespace()
    )

    bot.delete_message.assert_not_awaited()
    send.assert_awaited_once()
    assert state.msg_id == 9
    assert state.is_photo_msg is True
    assert state.is_rich_media_msg is False
    assert state.last_pane_hash == "pane-hash"
    assert state.last_photo_edit_ts == 10.0


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
    final_edit = AsyncMock(return_value=True)
    ensure_seeded = AsyncMock(return_value=None)
    monkeypatch.setattr(card_updates, "get_card_state", lambda _uid, _sess: state)
    monkeypatch.setattr(card_updates, "_should_buffer", lambda *_args: False)
    monkeypatch.setattr(
        card_updates,
        "_legacy",
        lambda name: {
            "_ensure_seeded": ensure_seeded,
            "_render_card": lambda *_args, **_kwargs: "rendered final",
            "build_footer_keyboard": lambda *_args, **_kwargs: None,
            "_edit_card": final_edit,
        }[name],
    )
    session = SimpleNamespace(id="s1", window_id="")

    finalize = asyncio.create_task(
        card_updates.finalize_task(SimpleNamespace(), 42, session, "final answer")
    )
    await asyncio.sleep(0)
    edited_before_release = final_edit.await_count
    release.set()
    await asyncio.gather(pending, finalize, return_exceptions=True)

    assert was_cancelled is False
    assert edited_before_release == 0
    final_edit.assert_awaited_once()
    assert state.pending_edit is None


@pytest.mark.parametrize(
    ("kind", "file_id"),
    [(CarrierKind.RICH_MEDIA, "pane-file"), (CarrierKind.LEGACY_PHOTO, "")],
)
def test_carrier_rebinding_clears_previous_media_kind(
    monkeypatch: pytest.MonkeyPatch,
    kind: CarrierKind,
    file_id: str,
) -> None:
    """Media flags describe the newly bound message, never an old carrier."""
    key = (42, "to")
    state = CardState()
    bind_carrier(
        state,
        8,
        kind,
        rich_media_file_id=file_id,
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
