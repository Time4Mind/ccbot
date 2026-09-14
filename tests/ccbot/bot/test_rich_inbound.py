from telegram import Message
from types import SimpleNamespace
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

import pytest

from ccbot.bot import _messages_media
from ccbot.bot._messages_media import _incoming_rich_text


def test_unknown_bot_api_rich_message_is_recovered_from_api_kwargs() -> None:
    msg = Message.de_json(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Artem"},
            "rich_message": {"markdown": "**Ready** &lt; 5 min"},
        },
        None,
    )

    assert msg is not None
    assert msg.text is None
    assert _incoming_rich_text(msg) == "**Ready** < 5 min"


def test_rich_blocks_are_flattened_when_markdown_is_absent() -> None:
    msg = Message.de_json(
        {
            "message_id": 2,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Artem"},
            "rich_message": {
                "blocks": [
                    {"type": "paragraph", "text": "First"},
                    {"type": "blockquote", "blocks": [{"text": "Second"}]},
                ]
            },
        },
        None,
    )

    assert msg is not None
    assert _incoming_rich_text(msg) == "First\nSecond"


@pytest.mark.asyncio
async def test_rich_message_is_delivered_to_active_session(monkeypatch) -> None:
    msg = Message.de_json(
        {
            "message_id": 3,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Artem"},
            "rich_message": {"markdown": "**Bot answer**"},
        },
        None,
    )
    assert msg is not None
    update = SimpleNamespace(message=msg, effective_user=msg.from_user)
    context = SimpleNamespace(bot=object())
    sess = SimpleNamespace(id="s1")
    sent = AsyncMock(return_value=(True, "ok"))

    class Repost:
        def commit(self) -> None:
            pass

    @asynccontextmanager
    async def bracket(*_args):
        yield Repost()

    monkeypatch.setattr(_messages_media, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(_messages_media, "active_window", lambda _uid: "@1")
    monkeypatch.setattr(
        _messages_media,
        "_await_prior_voice",
        AsyncMock(return_value=True),
        raising=False,
    )
    monkeypatch.setattr(_messages_media, "fire_typing", AsyncMock())
    monkeypatch.setattr(
        _messages_media,
        "_intercept_if_pending_ui",
        AsyncMock(return_value=False),
        raising=False,
    )
    monkeypatch.setattr(_messages_media, "_card_repost_bracket", bracket, raising=False)
    monkeypatch.setattr(
        _messages_media, "_send_with_delivery_proof", sent, raising=False
    )
    monkeypatch.setattr(
        _messages_media,
        "tmux_manager",
        SimpleNamespace(find_window_by_id=AsyncMock(return_value=SimpleNamespace())),
    )
    monkeypatch.setattr(
        _messages_media,
        "session_manager",
        SimpleNamespace(
            get_display_name=lambda _wid: "session",
            find_session_by_window=lambda _wid: sess,
            touch_session=lambda _sid: None,
        ),
    )

    assert await _messages_media.unsupported_content_handler(update, context)
    sent.assert_awaited_once_with("@1", "**Bot answer**", sess)
