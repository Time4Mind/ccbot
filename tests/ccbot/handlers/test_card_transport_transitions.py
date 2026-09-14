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
from ccbot.handlers.card_model import CardState, CarrierKind, TurnPhase


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
