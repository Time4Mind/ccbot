from telegram import Message
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from ccbot.bot import messages
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

    monkeypatch.setattr(messages, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(messages, "active_window", lambda _uid: "@1")
    monkeypatch.setattr(
        messages,
        "_await_prior_voice",
        AsyncMock(return_value=True),
        raising=False,
    )
    monkeypatch.setattr(messages, "fire_typing", AsyncMock())
    monkeypatch.setattr(
        messages,
        "prepare_request_for_dispatch",
        AsyncMock(
            return_value=SimpleNamespace(
                text="**Bot answer**", confirm_delivery=lambda: None
            )
        ),
    )
    monkeypatch.setattr(
        messages,
        "_intercept_if_pending_ui",
        AsyncMock(return_value=False),
        raising=False,
    )
    monkeypatch.setattr(messages, "_card_repost_bracket", bracket, raising=False)
    monkeypatch.setattr(messages, "_send_with_delivery_proof", sent, raising=False)
    monkeypatch.setattr(
        messages,
        "tmux_manager",
        SimpleNamespace(find_window_by_id=AsyncMock(return_value=SimpleNamespace())),
    )
    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(
            get_display_name=lambda _wid: "session",
            find_session_by_window=lambda _wid: sess,
            touch_session=lambda _sid: None,
        ),
    )

    assert await messages.unsupported_content_handler(update, context)
    sent.assert_awaited_once_with("@1", "**Bot answer**", sess)


@pytest.mark.asyncio
async def test_forwarded_rich_message_delivers_embedded_photo(
    monkeypatch, tmp_path
) -> None:
    msg = Message.de_json(
        {
            "message_id": 4,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Artem"},
            "forward_origin": {
                "type": "user",
                "date": 0,
                "sender_user": {
                    "id": 99,
                    "is_bot": True,
                    "first_name": "Source",
                    "username": "source_bot",
                },
            },
            "rich_message": {
                "blocks": [
                    {"type": "paragraph", "text": "Review this"},
                    {
                        "type": "photo",
                        "photo": [
                            {
                                "file_id": "small-id",
                                "file_unique_id": "photo-unique",
                                "width": 90,
                                "height": 90,
                            },
                            {
                                "file_id": "large-id",
                                "file_unique_id": "photo-unique",
                                "width": 1280,
                                "height": 720,
                            },
                        ],
                    },
                ]
            },
        },
        None,
    )
    assert msg is not None

    async def download_to_drive(path: Path) -> None:
        path.write_bytes(b"image-bytes")

    get_file = AsyncMock(
        return_value=SimpleNamespace(download_to_drive=download_to_drive)
    )
    context = SimpleNamespace(bot=SimpleNamespace(get_file=get_file))
    update = SimpleNamespace(message=msg, effective_user=msg.from_user)
    sess = SimpleNamespace(id="s1", workdir=str(tmp_path))
    sent = AsyncMock(return_value=(True, "ok"))

    class Repost:
        def commit(self) -> None:
            pass

    @asynccontextmanager
    async def bracket(*_args):
        yield Repost()

    monkeypatch.setattr(messages, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(messages, "active_window", lambda _uid: "@1")
    monkeypatch.setattr(
        messages,
        "_await_prior_voice",
        AsyncMock(return_value=True),
        raising=False,
    )
    monkeypatch.setattr(messages, "fire_typing", AsyncMock())
    monkeypatch.setattr(
        messages,
        "_intercept_if_pending_ui",
        AsyncMock(return_value=False),
        raising=False,
    )
    monkeypatch.setattr(messages, "_card_repost_bracket", bracket, raising=False)
    monkeypatch.setattr(messages, "_send_with_delivery_proof", sent, raising=False)
    monkeypatch.setattr(
        messages,
        "tmux_manager",
        SimpleNamespace(find_window_by_id=AsyncMock(return_value=SimpleNamespace())),
    )
    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(
            get_display_name=lambda _wid: "session",
            find_session_by_window=lambda _wid: sess,
            get_user_settings=lambda _uid: {
                "preprocessing_mode": "all",
                "preprocessing_instruction": "",
            },
            touch_session=lambda _sid: None,
            save_state=lambda: None,
        ),
    )

    assert await messages.unsupported_content_handler(update, context)

    get_file.assert_awaited_once_with("large-id")
    delivered = sent.await_args.args[1]
    assert delivered.startswith("[forwarded from @source_bot]\nReview this")
    assert ".ccbot-inbox/" in delivered
    saved = list((tmp_path / ".ccbot-inbox").glob("*-photo-unique.jpg"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"image-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("media_kind", ["photo", "document"])
async def test_direct_media_caption_is_preprocessed_before_delivery(
    monkeypatch, tmp_path, media_kind
) -> None:
    downloaded = SimpleNamespace(download_to_drive=AsyncMock())
    media = SimpleNamespace(
        file_unique_id="media-id",
        file_name="notes.txt",
        get_file=AsyncMock(return_value=downloaded),
    )
    msg = SimpleNamespace(
        caption="ну проверь файл",
        photo=[media] if media_kind == "photo" else None,
        document=media if media_kind == "document" else None,
        forward_origin=None,
        via_bot=None,
    )
    update = SimpleNamespace(
        message=msg,
        effective_user=SimpleNamespace(id=42),
    )
    context = SimpleNamespace(bot=object())
    sess = SimpleNamespace(id="s1", workdir=str(tmp_path))
    prepared = SimpleNamespace(
        text="Проверь файл",
        confirm_delivery=MagicMock(),
    )
    preprocess = AsyncMock(return_value=prepared)
    forward = AsyncMock(return_value=(True, "ok"))

    class Repost:
        def commit(self) -> None:
            pass

    @asynccontextmanager
    async def bracket(*_args):
        yield Repost()

    monkeypatch.setattr(messages, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(messages, "active_window", lambda _uid: "@1")
    monkeypatch.setattr(messages, "_await_prior_voice", AsyncMock(return_value=True))
    monkeypatch.setattr(
        messages, "_intercept_if_pending_ui", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(messages, "_card_repost_bracket", bracket)
    monkeypatch.setattr(messages, "prepare_request_for_dispatch", preprocess)
    monkeypatch.setattr(messages, "_forward_inbox_file", forward)
    monkeypatch.setattr(
        messages,
        "save_inbox_file",
        AsyncMock(return_value=tmp_path / "saved-file"),
    )
    monkeypatch.setattr(
        messages,
        "tmux_manager",
        SimpleNamespace(find_window_by_id=AsyncMock(return_value=SimpleNamespace())),
    )
    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(
            get_display_name=lambda _wid: "session",
            find_session_by_window=lambda _wid: sess,
        ),
    )

    handler = (
        messages.photo_handler if media_kind == "photo" else messages.document_handler
    )
    assert await handler(update, context)

    preprocess.assert_awaited_once_with(
        update, context, 42, "@1", "ну проверь файл", input_kind="text"
    )
    assert forward.await_args.args[4] == "Проверь файл"
    assert forward.await_args.args[5] == media_kind.replace("photo", "image")
    assert forward.await_args.args[6] is context.bot
    prepared.confirm_delivery.assert_called_once_with()


@pytest.mark.asyncio
async def test_image_only_rich_message_does_not_run_text_preprocessing(
    monkeypatch, tmp_path
) -> None:
    msg = Message.de_json(
        {
            "message_id": 5,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Artem"},
            "rich_message": {
                "blocks": [
                    {
                        "type": "photo",
                        "photo": [
                            {
                                "file_id": "image-id",
                                "file_unique_id": "image-unique",
                                "width": 100,
                                "height": 100,
                            }
                        ],
                    }
                ]
            },
        },
        None,
    )
    assert msg is not None
    update = SimpleNamespace(message=msg, effective_user=msg.from_user)
    context = SimpleNamespace(bot=object())
    sess = SimpleNamespace(id="s1", workdir=str(tmp_path))
    preprocess = AsyncMock()
    sent = AsyncMock(return_value=(True, "ok"))

    class Repost:
        def commit(self) -> None:
            pass

    @asynccontextmanager
    async def bracket(*_args):
        yield Repost()

    monkeypatch.setattr(messages, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(messages, "active_window", lambda _uid: "@1")
    monkeypatch.setattr(messages, "_await_prior_voice", AsyncMock(return_value=True))
    monkeypatch.setattr(
        messages, "_intercept_if_pending_ui", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(messages, "_card_repost_bracket", bracket)
    monkeypatch.setattr(messages, "prepare_request_for_dispatch", preprocess)
    monkeypatch.setattr(messages, "fire_typing", AsyncMock())
    monkeypatch.setattr(messages, "_send_with_delivery_proof", sent)
    monkeypatch.setattr(
        messages,
        "_save_rich_photos",
        AsyncMock(return_value=[tmp_path / "saved-image.jpg"]),
        raising=False,
    )
    monkeypatch.setattr(
        messages,
        "tmux_manager",
        SimpleNamespace(find_window_by_id=AsyncMock(return_value=SimpleNamespace())),
    )
    monkeypatch.setattr(
        messages,
        "session_manager",
        SimpleNamespace(
            get_display_name=lambda _wid: "session",
            find_session_by_window=lambda _wid: sess,
            touch_session=lambda _sid: None,
        ),
    )

    assert await messages.unsupported_content_handler(update, context)
    preprocess.assert_not_awaited()
    assert sent.await_args.args[1] == ".ccbot-inbox/saved-image.jpg"
