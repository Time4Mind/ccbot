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
from ccbot.tmux_manager import tmux_manager


class _LiveControlProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


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


@pytest.mark.asyncio
async def test_post_shutdown_stops_persistent_tmux_control_process(
    monkeypatch,
) -> None:
    process = _LiveControlProcess()
    monkeypatch.setattr(tmux_manager._control_client, "proc", process)
    monkeypatch.setattr(_app_lifecycle, "session_monitor", None)
    for name in (
        "shutdown_auth_flows",
        "shutdown_inbound_queues",
        "shutdown_card_surface_tasks",
        "shutdown_file_deliveries",
    ):
        monkeypatch.setattr(_app_lifecycle, name, AsyncMock())
    monkeypatch.setattr(
        "ccbot.default_session.shutdown_default_session_tasks", AsyncMock()
    )
    monkeypatch.setattr(
        "ccbot.request_preprocessing.prompt_preprocessor.close", AsyncMock()
    )
    monkeypatch.setattr("ccbot.handlers.history.cancel_pending_prewarm", AsyncMock())
    monkeypatch.setattr(
        "ccbot.handlers.directory_browser.shutdown_directory_recency", AsyncMock()
    )
    monkeypatch.setattr(
        "ccbot.handlers.notifications.cancel_pending_card_edits", AsyncMock()
    )
    monkeypatch.setattr(_app_lifecycle.session_manager, "save_state", lambda: None)

    await _app_lifecycle.post_shutdown(SimpleNamespace())

    assert process.terminated
    assert tmux_manager._control_client.proc is None
    _app_lifecycle.shutdown_file_deliveries.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_shutdown_releases_lock_before_directory_worker_cleanup(
    monkeypatch,
) -> None:
    lock_released = False

    def release_lock() -> None:
        nonlocal lock_released
        lock_released = True

    async def shutdown_directory_recency() -> None:
        assert lock_released

    monkeypatch.setattr("ccbot.main.release_singleton_lock", release_lock)
    monkeypatch.setattr(_app_lifecycle, "session_monitor", None)
    for name in (
        "shutdown_auth_flows",
        "shutdown_inbound_queues",
        "shutdown_card_surface_tasks",
        "shutdown_file_deliveries",
    ):
        monkeypatch.setattr(_app_lifecycle, name, AsyncMock())
    monkeypatch.setattr(
        "ccbot.default_session.shutdown_default_session_tasks", AsyncMock()
    )
    monkeypatch.setattr(
        "ccbot.request_preprocessing.prompt_preprocessor.close", AsyncMock()
    )
    monkeypatch.setattr("ccbot.handlers.history.cancel_pending_prewarm", AsyncMock())
    monkeypatch.setattr(
        "ccbot.handlers.directory_browser.shutdown_directory_recency",
        shutdown_directory_recency,
    )
    monkeypatch.setattr(
        "ccbot.handlers.notifications.cancel_pending_card_edits", AsyncMock()
    )
    monkeypatch.setattr(tmux_manager, "close_control_client", AsyncMock())
    monkeypatch.setattr(_app_lifecycle.session_manager, "save_state", lambda: None)

    await _app_lifecycle.post_shutdown(SimpleNamespace())

    assert lock_released


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


def test_bot_keeps_global_update_processing_sequential(monkeypatch) -> None:
    monkeypatch.setattr(config, "telegram_bot_token", "123456:ABCDEF")

    application = create_bot()

    assert application.update_processor.max_concurrent_updates == 1


@pytest.mark.parametrize("proxy", ["", "http://127.0.0.1:8080"])
def test_polling_transport_is_instrumented_with_and_without_proxy(
    monkeypatch, proxy
) -> None:
    monkeypatch.setattr(config, "telegram_bot_token", "123456:ABCDEF")
    monkeypatch.setattr(config, "tg_proxy_url", proxy)

    application = create_bot()

    polling_request = application.bot._request[0]
    assert type(polling_request).__name__ == "PollingHeartbeatRequest"
    assert polling_request.delegate is not application.bot.request
    assert polling_request.delegate._client.timeout.connect == 10.0
    assert polling_request.delegate._client.timeout.pool == 10.0
    assert polling_request.delegate._client._transport._pool._max_connections == 4
