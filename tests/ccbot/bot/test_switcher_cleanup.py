"""Session-switch cleanup avoids redundant Telegram mutations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import switcher


@pytest.mark.asyncio
async def test_stale_orphan_keyboard_is_not_cleaned_twice(monkeypatch) -> None:
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(return_value=True))
    monkeypatch.setattr(
        switcher.session_manager, "get_last_switcher_msg", lambda _uid: 99
    )

    assert not await switcher._strip_orphan_switcher_if_current(bot, 42, 88)
    bot.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_orphan_keyboard_is_stripped(monkeypatch) -> None:
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(return_value=True))
    monkeypatch.setattr(
        switcher.session_manager, "get_last_switcher_msg", lambda _uid: 88
    )

    assert await switcher._strip_orphan_switcher_if_current(bot, 42, 88)
    bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=42, message_id=88, reply_markup=None
    )
