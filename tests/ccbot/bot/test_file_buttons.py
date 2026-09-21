from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import rich
from ccbot.bot.callbacks import file_buttons
from ccbot.file_delivery import FileDeliveryManager


@pytest.mark.asyncio
async def test_file_button_sends_registered_file(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    rendered = rich.to_rich_markdown(str(path))
    token = re.search(r'data="file:([0-9a-f]+)"', rendered)
    assert token is not None
    query = SimpleNamespace(data=f"file:{token.group(1)}", answer=AsyncMock())
    delivered: list[bytes] = []

    async def send_document(**kwargs):
        delivered.append(kwargs["document"].read())

    bot = SimpleNamespace(
        send_document=AsyncMock(side_effect=send_document),
        send_message=AsyncMock(),
    )
    manager = FileDeliveryManager(staging_dir=tmp_path / "staging")
    monkeypatch.setattr(file_buttons, "file_delivery_manager", manager)

    handled = await file_buttons.handle(
        query,
        SimpleNamespace(bot=bot),
        SimpleNamespace(id=42),
    )
    await manager.wait_for_idle()

    assert handled is True
    query.answer.assert_awaited_once_with()
    bot.send_document.assert_awaited_once()
    assert bot.send_document.await_args.kwargs["chat_id"] == 42
    assert bot.send_document.await_args.kwargs["filename"] == "result.json"
    assert delivered == [b'{"ok": true}']


@pytest.mark.asyncio
async def test_unknown_file_button_is_rejected() -> None:
    query = SimpleNamespace(data="file:missing", answer=AsyncMock())
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())

    handled = await file_buttons.handle(
        query,
        SimpleNamespace(bot=bot),
        SimpleNamespace(id=42),
    )

    assert handled is True
    query.answer.assert_awaited_once_with("Файл больше недоступен", show_alert=True)
    bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_file_removed_before_tap_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "gone.zip"
    path.write_bytes(b"payload")
    rendered = rich.to_rich_markdown(str(path))
    token = re.search(r'data="file:([0-9a-f]+)"', rendered)
    assert token is not None
    path.unlink()
    query = SimpleNamespace(data=f"file:{token.group(1)}", answer=AsyncMock())
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())

    assert await file_buttons.handle(
        query, SimpleNamespace(bot=bot), SimpleNamespace(id=42)
    )

    query.answer.assert_awaited_once_with("Файл больше недоступен", show_alert=True)
    bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_callback_returns_while_telegram_upload_is_blocked(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "large.zip"
    path.write_bytes(b"payload")
    rendered = rich.to_rich_markdown(str(path))
    token = re.search(r'data="file:([0-9a-f]+)"', rendered)
    assert token is not None
    query = SimpleNamespace(data=f"file:{token.group(1)}", answer=AsyncMock())
    upload_started = asyncio.Event()
    release_upload = asyncio.Event()

    async def send_document(**_kwargs):
        upload_started.set()
        await release_upload.wait()

    bot = SimpleNamespace(
        send_document=AsyncMock(side_effect=send_document),
        send_message=AsyncMock(),
    )
    manager = FileDeliveryManager(staging_dir=tmp_path / "staging")
    monkeypatch.setattr(file_buttons, "file_delivery_manager", manager)
    callback = asyncio.create_task(
        file_buttons.handle(query, SimpleNamespace(bot=bot), SimpleNamespace(id=42))
    )
    await upload_started.wait()

    try:
        query.answer.assert_awaited_once_with()
        assert callback.done(), "file upload still owns the Telegram update handler"
    finally:
        release_upload.set()
        await callback
        await manager.wait_for_idle()


@pytest.mark.asyncio
async def test_repeated_file_taps_send_one_document(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "result.zip"
    path.write_bytes(b"payload")
    other_path = tmp_path / "other.zip"
    other_path.write_bytes(b"other")
    rendered = rich.to_rich_markdown(str(path))
    token = re.search(r'data="file:([0-9a-f]+)"', rendered)
    assert token is not None
    other_rendered = rich.to_rich_markdown(str(other_path))
    other_token = re.search(r'data="file:([0-9a-f]+)"', other_rendered)
    assert other_token is not None
    upload_started = asyncio.Event()
    release_upload = asyncio.Event()

    async def send_document(**_kwargs):
        upload_started.set()
        await release_upload.wait()

    bot = SimpleNamespace(
        send_document=AsyncMock(side_effect=send_document), send_message=AsyncMock()
    )
    manager = FileDeliveryManager(staging_dir=tmp_path / "staging")
    monkeypatch.setattr(file_buttons, "file_delivery_manager", manager)

    for _ in range(2):
        query = SimpleNamespace(data=f"file:{token.group(1)}", answer=AsyncMock())
        assert await file_buttons.handle(
            query, SimpleNamespace(bot=bot), SimpleNamespace(id=42)
        )
        query.answer.assert_awaited_once_with()
    busy_query = SimpleNamespace(
        data=f"file:{other_token.group(1)}", answer=AsyncMock()
    )
    assert await file_buttons.handle(
        busy_query, SimpleNamespace(bot=bot), SimpleNamespace(id=42)
    )
    busy_query.answer.assert_awaited_once_with()
    await upload_started.wait()
    release_upload.set()
    await manager.wait_for_idle()

    bot.send_document.assert_awaited_once()
    bot.send_message.assert_awaited_once()
