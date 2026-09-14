"""Rich-media live-card transport keeps the pane as its final block."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from ccbot.config import config
from ccbot.handlers import card_rich_media, card_transport, message_sender
from ccbot.handlers.card_model import CardState
from ccbot.handlers.card_types import TurnPhase
from ccbot.session_models import Session


@pytest.mark.asyncio
async def test_send_uploads_pane_and_returns_reusable_file_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send = AsyncMock(return_value=SimpleNamespace(message_id=17))
    monkeypatch.setattr(config, "rich_messages", True)
    monkeypatch.setattr(card_rich_media.rich, "send_rich_message", send)
    monkeypatch.setattr(
        card_rich_media.rich,
        "extract_rich_photo_file_id",
        lambda _message: "pane-file-id",
    )

    text = "**answer**\n\ncontext: 42%\n\n─── фон ───"
    state = CardState(media_anchor_offset=len("**answer**"))
    result = await card_rich_media.send_rich_media_card(
        SimpleNamespace(), 42, state, text, b"png", reply_markup=None
    )

    assert result is not None
    assert result.message.message_id == 17
    assert result.photo_file_id == "pane-file-id"
    assert send.await_args.kwargs["photo"] == b"png"
    assert send.await_args.kwargs["disable_notification"] is True
    markdown = send.await_args.args[2]
    assert markdown.index(card_rich_media.rich.RICH_PHOTO_ANCHOR) < markdown.index(
        "context: 42%"
    )
    assert (
        f"{card_rich_media._MEDIA_SPACER}\n\n"
        f"{card_rich_media.rich.RICH_PHOTO_ANCHOR}\n\n"
        f"{card_rich_media._MEDIA_SPACER}"
    ) in markdown
    assert "<br>" not in markdown


@pytest.mark.asyncio
async def test_send_preserves_legacy_path_when_rich_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send = AsyncMock()
    monkeypatch.setattr(config, "rich_messages", False)
    monkeypatch.setattr(card_rich_media.rich, "send_rich_message", send)

    result = await card_rich_media.send_rich_media_card(
        SimpleNamespace(), 42, CardState(), "answer", b"png", reply_markup=None
    )

    assert result is None
    send.assert_not_awaited()


def _wire_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        card_rich_media, "lookup_session_for_message", lambda _uid, _mid: "s1"
    )
    monkeypatch.setattr(
        card_rich_media.session_manager,
        "get_session",
        lambda _sid: SimpleNamespace(window_id="ccbot:1"),
    )


def test_confirmed_screenshot_cache_is_persisted_per_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = Session(id="s1", name="one")
    save = MagicMock()
    monkeypatch.setattr(card_rich_media.session_manager, "save_state", save)
    monkeypatch.setattr(card_rich_media.time, "time", lambda: 123.5)

    monkeypatch.setattr(
        card_rich_media.session_manager,
        "get_user_settings",
        lambda _uid: {"screenshot_capture_kib": 64, "screenshot_profile": "compact8"},
    )

    card_rich_media.persist_session_screenshot(sess, 42, "pane-hash", "photo-id")

    assert sess.screenshot_file_id == "photo-id"
    assert sess.screenshot_pane_hash == "pane-hash"
    assert sess.screenshot_cached_at == 123.5
    assert sess.screenshot_user_id == 42
    assert sess.screenshot_capture_kib == 64
    assert sess.screenshot_profile == "compact8"
    save.assert_called_once_with()


def test_unchanged_screenshot_only_refreshes_timestamp_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = Session(
        id="s1",
        name="one",
        screenshot_file_id="photo-id",
        screenshot_pane_hash="pane-hash",
        screenshot_cached_at=100.0,
        screenshot_user_id=42,
        screenshot_capture_kib=64,
        screenshot_profile="compact8",
    )
    save = MagicMock()
    monkeypatch.setattr(card_rich_media.session_manager, "save_state", save)
    monkeypatch.setattr(card_rich_media.time, "time", lambda: 111.0)
    monkeypatch.setattr(
        card_rich_media.session_manager,
        "get_user_settings",
        lambda _uid: {"screenshot_capture_kib": 64, "screenshot_profile": "compact8"},
    )

    card_rich_media.persist_session_screenshot(sess, 42, "pane-hash", "photo-id")

    assert sess.screenshot_cached_at == 111.0
    save.assert_not_called()


@pytest.mark.asyncio
async def test_interactive_text_edit_reuses_cached_photo_without_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_session(monkeypatch)
    edit = AsyncMock(return_value=None)
    capture = AsyncMock(return_value=(b"png", "pane-hash"))
    monkeypatch.setattr(card_rich_media.rich, "edit_rich_message", edit)
    monkeypatch.setattr(card_rich_media, "_capture_pane_png", capture)
    monkeypatch.setattr(card_rich_media.time, "monotonic", lambda: 10.0)
    state = CardState(
        msg_id=9,
        is_rich_media_msg=True,
        rich_media_file_id="cached-pane",
        last_pane_hash="hash-a",
        last_photo_edit_ts=1.0,
    )

    assert await card_rich_media.edit_rich_media_card(
        SimpleNamespace(),
        42,
        state,
        text="next",
        reply_markup=None,
        min_photo_interval=2.5,
        refresh_pane=False,
    )

    capture.assert_not_awaited()
    assert edit.await_args.kwargs["photo"] == "cached-pane"


@pytest.mark.asyncio
async def test_changed_pane_uploads_and_atomically_replaces_file_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_session(monkeypatch)
    response = {
        "rich_message": {
            "blocks": [
                {
                    "type": "photo",
                    "photo": [{"file_id": "new-pane", "width": 100, "height": 100}],
                }
            ]
        }
    }
    edit = AsyncMock(return_value=response)
    monkeypatch.setattr(card_rich_media.rich, "edit_rich_message", edit)
    monkeypatch.setattr(
        card_rich_media, "_capture_pane_png", AsyncMock(return_value=(b"new", "hash-b"))
    )
    monkeypatch.setattr(card_rich_media.time, "monotonic", lambda: 10.0)
    state = CardState(
        msg_id=9,
        is_rich_media_msg=True,
        rich_media_file_id="old-pane",
        last_pane_hash="hash-a",
        last_photo_edit_ts=1.0,
    )

    assert await card_rich_media.edit_rich_media_card(
        SimpleNamespace(),
        42,
        state,
        text="next command",
        reply_markup=None,
        min_photo_interval=2.5,
    )

    assert edit.await_args.kwargs["photo"] == b"new"
    assert state.rich_media_file_id == "new-pane"
    assert state.last_pane_hash == "hash-b"
    assert state.last_photo_edit_ts == 10.0


@pytest.mark.asyncio
async def test_previous_exact_png_reuses_bounded_session_file_id_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_session(monkeypatch)
    edit = AsyncMock(return_value=None)
    monkeypatch.setattr(card_rich_media.rich, "edit_rich_message", edit)
    monkeypatch.setattr(
        card_rich_media,
        "_capture_pane_png",
        AsyncMock(return_value=(b"same-png", "hash-b")),
    )
    monkeypatch.setattr(card_rich_media.time, "monotonic", lambda: 10.0)
    state = CardState(
        msg_id=9,
        is_rich_media_msg=True,
        rich_media_file_id="current-pane",
        last_pane_hash="hash-a",
        last_photo_edit_ts=1.0,
        rich_media_cache=[("hash-b", "cached-pane-b")],
    )

    assert await card_rich_media.edit_rich_media_card(
        SimpleNamespace(),
        42,
        state,
        text="back to prior pane",
        reply_markup=None,
        min_photo_interval=2.5,
    )

    assert edit.await_args.kwargs["photo"] == "cached-pane-b"
    assert state.rich_media_file_id == "cached-pane-b"
    assert state.last_pane_hash == "hash-b"


@pytest.mark.asyncio
async def test_lost_rich_carrier_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_session(monkeypatch)
    monkeypatch.setattr(
        card_rich_media.rich,
        "edit_rich_message",
        AsyncMock(side_effect=BadRequest("Message to edit not found")),
    )
    monkeypatch.setattr(card_rich_media.time, "monotonic", lambda: 2.0)
    state = CardState(
        msg_id=9,
        is_rich_media_msg=True,
        rich_media_file_id="cached-pane",
        last_photo_edit_ts=1.0,
    )

    assert not await card_rich_media.edit_rich_media_card(
        SimpleNamespace(),
        42,
        state,
        text="next",
        reply_markup=None,
        min_photo_interval=2.5,
    )
    assert state.msg_id is None


@pytest.mark.asyncio
async def test_final_edit_keeps_latest_rich_pane_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rich_edit = AsyncMock(return_value=True)
    media_edit = AsyncMock(return_value=True)
    monkeypatch.setattr(message_sender, "try_rich_edit", rich_edit)
    monkeypatch.setattr(card_transport, "edit_rich_media_card", media_edit)
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda _uid: True)
    state = CardState(
        msg_id=9,
        is_rich_media_msg=True,
        rich_media_file_id="pane-file",
        last_pane_hash="pane-hash",
        last_photo_edit_ts=10.0,
        turn_phase=TurnPhase.IDLE,
    )

    assert await card_transport._edit_card_unlocked(
        SimpleNamespace(),
        42,
        state,
        text="final answer\n\ncontext: 42%",
        reply_markup=SimpleNamespace(),
    )

    rich_edit.assert_not_awaited()
    media_edit.assert_awaited_once()
    assert state.is_rich_media_msg is True
    assert state.rich_media_file_id == "pane-file"
    assert state.last_pane_hash == "pane-hash"


@pytest.mark.asyncio
async def test_disabling_screenshot_removes_media_from_same_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rich_edit = AsyncMock(return_value=True)
    monkeypatch.setattr(message_sender, "try_rich_edit", rich_edit)
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda _uid: False)
    state = CardState(
        msg_id=9,
        is_rich_media_msg=True,
        rich_media_file_id="pane-file",
        last_pane_hash="pane-hash",
        last_photo_edit_ts=10.0,
        turn_phase=TurnPhase.IDLE,
    )

    assert await card_transport._edit_card_unlocked(
        SimpleNamespace(),
        42,
        state,
        text="final answer",
        reply_markup=SimpleNamespace(),
    )

    rich_edit.assert_awaited_once()
    assert state.msg_id == 9
    assert state.is_rich_media_msg is False
    assert state.rich_media_file_id == ""


@pytest.mark.asyncio
async def test_rich_media_failure_degrades_same_carrier_to_text_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_edit = AsyncMock(return_value=False)
    text_edit = AsyncMock(return_value=True)
    monkeypatch.setattr(card_transport, "edit_rich_media_card", media_edit)
    monkeypatch.setattr(message_sender, "try_rich_edit", text_edit)
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda _uid: True)
    state = CardState(
        msg_id=9,
        is_rich_media_msg=True,
        rich_media_file_id="pane-file",
    )

    assert await card_transport._edit_card_unlocked(
        SimpleNamespace(), 42, state, text="new text", reply_markup=SimpleNamespace()
    )

    assert state.msg_id == 9
    assert state.is_rich_media_msg is False
    text_edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_final_send_keeps_latest_pane_when_screenshots_are_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = AsyncMock(return_value=(b"png", "pane-hash"))
    send_text = AsyncMock(return_value=SimpleNamespace(message_id=18))
    send_rich = AsyncMock(
        return_value=card_rich_media.RichCardSend(
            message=SimpleNamespace(message_id=17), photo_file_id="pane-file"
        )
    )
    monkeypatch.setattr(card_transport, "_inline_screens_enabled", lambda _uid: True)
    monkeypatch.setattr(card_transport, "_capture_pane_png", capture)
    monkeypatch.setattr(message_sender, "send_with_fallback", send_text)
    monkeypatch.setattr(card_transport, "send_rich_media_card", send_rich)
    monkeypatch.setattr(card_transport, "_strip_stale_switchers", AsyncMock())
    monkeypatch.setattr(card_transport, "_register_msg", lambda *_args: None)
    monkeypatch.setattr(
        card_transport.session_manager, "set_last_switcher_msg", lambda *_args: None
    )
    monkeypatch.setattr(
        card_transport.session_manager, "set_card_msg", lambda *_args: None
    )
    state = CardState(turn_phase=TurnPhase.IDLE)
    session = SimpleNamespace(id="s1", window_id="@1")

    await card_transport._send_card_locked(
        SimpleNamespace(),
        42,
        session,
        state,
        text="final answer",
        reply_markup=SimpleNamespace(),
    )

    capture.assert_awaited_once()
    send_rich.assert_awaited_once()
    send_text.assert_not_awaited()
    assert state.msg_id == 17
    assert state.is_rich_media_msg is True
