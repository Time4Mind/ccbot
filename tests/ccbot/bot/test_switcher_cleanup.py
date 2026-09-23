"""Session-switch cleanup avoids redundant Telegram mutations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import switcher
from ccbot.handlers import card_registry


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


@pytest.mark.asyncio
async def test_inflight_switch_carrier_is_not_stripped(monkeypatch) -> None:
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(return_value=True))
    monkeypatch.setattr(
        card_registry.session_manager, "get_last_switcher_msg", lambda _uid: 88
    )
    card_registry.protect_switcher_carrier(42, 88)
    try:
        await card_registry._strip_stale_switchers(bot, 42, 99, "target")
        bot.edit_message_reply_markup.assert_not_awaited()
    finally:
        card_registry.release_switcher_carrier(42, 88)
