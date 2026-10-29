"""Session-switch cleanup avoids redundant Telegram mutations."""

from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import switcher
from ccbot.handlers import card_registry
from ccbot.handlers.card_types import CardState
from telegram.error import BadRequest, RetryAfter, NetworkError


@pytest.fixture(autouse=True)
def isolated_cleanup_cache(monkeypatch):
    monkeypatch.setattr(card_registry, "_stripped_switchers", {})
    monkeypatch.setattr(card_registry, "_switcher_retry_at", {})
    monkeypatch.setattr(card_registry, "_switcher_retry_attempts", {}, raising=False)
    monkeypatch.setattr(card_registry, "_switcher_user_retry_at", {}, raising=False)
    monkeypatch.setattr(card_registry, "_switcher_retry_tasks", {}, raising=False)


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


@pytest.mark.asyncio
async def test_old_switch_release_keeps_newer_carrier_protected(monkeypatch):
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(return_value=True))
    monkeypatch.setattr(
        card_registry.session_manager, "get_last_switcher_msg", lambda uid: 88
    )
    card_registry.protect_switcher_carrier(42, 88)
    card_registry.protect_switcher_carrier(42, 88)
    try:
        card_registry.release_switcher_carrier(42, 88)
        await card_registry._strip_stale_switchers(bot, 42, 99, "target")
        bot.edit_message_reply_markup.assert_not_awaited()
    finally:
        card_registry.release_switcher_carrier(42, 88)


@pytest.mark.asyncio
async def test_confirmed_stripped_card_is_not_rechecked_until_reactivated(monkeypatch):
    monkeypatch.setattr(card_registry, "_cards", {(42, "old"): CardState(msg_id=88)})
    monkeypatch.setattr(
        card_registry.session_manager, "get_last_switcher_msg", lambda _: None
    )
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(return_value=True))
    await card_registry._strip_stale_switchers(bot, 42, 99, "new")
    await card_registry._strip_stale_switchers(bot, 42, 99, "new")
    assert bot.edit_message_reply_markup.await_count == 1
    await card_registry._strip_stale_switchers(bot, 42, 88, "old")
    await card_registry._strip_stale_switchers(bot, 42, 99, "new")
    assert bot.edit_message_reply_markup.await_count == 2


@pytest.mark.asyncio
async def test_already_removed_keyboard_is_remembered(monkeypatch):
    monkeypatch.setattr(card_registry, "_cards", {(42, "old"): CardState(msg_id=88)})
    monkeypatch.setattr(
        card_registry.session_manager, "get_last_switcher_msg", lambda _: None
    )
    bot = SimpleNamespace(
        edit_message_reply_markup=AsyncMock(
            side_effect=BadRequest("Message is not modified")
        )
    )
    await card_registry._strip_stale_switchers(bot, 42, 99, "new")
    await card_registry._strip_stale_switchers(bot, 42, 99, "new")
    assert bot.edit_message_reply_markup.await_count == 1


@pytest.mark.asyncio
async def test_cleanup_stops_after_telegram_rate_limit(monkeypatch):
    monkeypatch.setattr(
        card_registry,
        "_cards",
        {(42, "one"): CardState(msg_id=88), (42, "two"): CardState(msg_id=89)},
    )
    monkeypatch.setattr(
        card_registry.session_manager, "get_last_switcher_msg", lambda _: None
    )
    bot = SimpleNamespace(
        edit_message_reply_markup=AsyncMock(side_effect=RetryAfter(120))
    )
    await card_registry._strip_stale_switchers(bot, 42, 99, "new")
    await card_registry._strip_stale_switchers(bot, 42, 99, "new")
    assert bot.edit_message_reply_markup.await_count == 1


@pytest.mark.asyncio
async def test_temporary_cleanup_error_is_retried_without_user_action(monkeypatch):
    monkeypatch.setattr(card_registry, "_cards", {(42, "old"): CardState(msg_id=88)})
    monkeypatch.setattr(
        card_registry, "_SWITCHER_TRANSIENT_RETRY_SECONDS", 0.01, raising=False
    )
    monkeypatch.setattr(
        card_registry.session_manager, "get_last_switcher_msg", lambda _: 99
    )
    monkeypatch.setattr(
        card_registry.session_manager,
        "get_active_session",
        lambda _: SimpleNamespace(id="new"),
    )
    bot = SimpleNamespace(
        edit_message_reply_markup=AsyncMock(
            side_effect=[NetworkError("temporary"), True]
        )
    )
    await card_registry._strip_stale_switchers(bot, 42, 99, "new")
    await asyncio.sleep(0.05)
    assert bot.edit_message_reply_markup.await_count == 2
