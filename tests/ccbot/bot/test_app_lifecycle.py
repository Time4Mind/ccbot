from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import BotCommand
from telegram.error import RetryAfter
from telegram.ext import CommandHandler

from ccbot.bot import _app_lifecycle
from ccbot.bot.app import create_bot
from ccbot.config import config
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


@pytest.mark.asyncio
async def test_published_command_menu_omits_hidden_commands(monkeypatch) -> None:
    """Commands remain callable, but only the compact menu is published."""
    sync = AsyncMock(side_effect=RuntimeError("stop after command sync"))
    monkeypatch.setattr(_app_lifecycle, "_sync_bot_commands", sync)
    monkeypatch.setattr(
        "ccbot.handlers.directory_browser.prewarm_directory_recency",
        lambda: None,
    )
    application = SimpleNamespace(bot=SimpleNamespace(username="ccbot"))

    with pytest.raises(RuntimeError, match="stop after command sync"):
        await _app_lifecycle.post_init(application)

    published = [command.command for command in sync.await_args.args[1]]
    assert published == ["menu", "help", "model"]
    assert not {"history", "done", "memory", "compact", "effort"} & set(published)


def test_removed_commands_have_no_dedicated_routes(monkeypatch) -> None:
    monkeypatch.setattr(config, "telegram_bot_token", "123456:ABCDEF")

    application = create_bot()
    registered = {
        command
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }

    assert not {"history", "done", "memory", "compact", "effort"} & registered
