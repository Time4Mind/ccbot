from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import rich
from ccbot.bot.callbacks import file_buttons


@pytest.mark.asyncio
async def test_file_button_sends_registered_file(tmp_path: Path) -> None:
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

    handled = await file_buttons.handle(
        query,
        SimpleNamespace(bot=bot),
        SimpleNamespace(id=42),
    )

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
