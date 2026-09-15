from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from telegram import BotCommand
from telegram.error import RetryAfter

from ccbot.bot._app_lifecycle import _sync_bot_commands


@pytest.mark.asyncio
async def test_bot_commands_are_not_written_when_unchanged() -> None:
    commands = [BotCommand("menu", "Open menu")]
    bot = AsyncMock()
    bot.get_my_commands.return_value = list(commands)

    assert not await _sync_bot_commands(bot, commands)

    bot.get_my_commands.assert_awaited_once()
    bot.delete_my_commands.assert_not_awaited()
    bot.set_my_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_commands_replace_stale_list_once() -> None:
    commands = [BotCommand("menu", "Open menu")]
    bot = AsyncMock()
    bot.get_my_commands.return_value = [BotCommand("old", "Old command")]

    assert await _sync_bot_commands(bot, commands)

    bot.get_my_commands.assert_awaited_once()
    bot.delete_my_commands.assert_not_awaited()
    bot.set_my_commands.assert_awaited_once_with(commands)


@pytest.mark.asyncio
async def test_command_sync_rate_limit_does_not_abort_bot_startup() -> None:
    commands = [BotCommand("menu", "Open menu")]
    bot = AsyncMock()
    bot.get_my_commands.side_effect = RetryAfter(120)

    assert not await _sync_bot_commands(bot, commands)

    bot.set_my_commands.assert_not_awaited()
