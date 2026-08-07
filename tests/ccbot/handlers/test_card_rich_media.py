"""Rich-media live-card transport keeps the pane as its final block."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from ccbot.config import config
from ccbot.handlers import card_rich_media
from ccbot.handlers.card_model import CardState


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


@pytest.mark.asyncio
async def test_interactive_text_edit_reuses_cached_photo_without_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_session(monkeypatch)
    edit = AsyncMock(return_value=None)
    capture = AsyncMock()
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
    response = {"rich_message": {"blocks": [{"type": "photo", "photo": [
        {"file_id": "new-pane", "width": 100, "height": 100}
    ]}]}}
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
