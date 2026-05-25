"""Tests for the per-user typing-indicator throttle.

``fire_typing`` collapses the many ``send_chat_action(TYPING)`` callers
behind a single per-user timestamp so a session emitting events at
1 Hz doesn't burn 5× the per-chat Telegram budget. Repeated calls
within ``TYPING_REFRESH_INTERVAL`` of the last successful fire are
silent no-ops. Was a measurable contributor to the 429 ``Retry after
71 s`` bans before this throttle landed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import typing as typing_mod
from ccbot.handlers.typing import TYPING_REFRESH_INTERVAL, fire_typing


@pytest.fixture(autouse=True)
def _clear_state():
    typing_mod._last_fired.clear()
    yield
    typing_mod._last_fired.clear()


@pytest.mark.asyncio
async def test_first_call_fires() -> None:
    bot = AsyncMock()
    sent = await fire_typing(bot, 42, "test")
    assert sent is True
    bot.send_chat_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_call_within_window_is_suppressed() -> None:
    bot = AsyncMock()
    await fire_typing(bot, 42, "test")
    bot.send_chat_action.reset_mock()
    sent = await fire_typing(bot, 42, "test")
    assert sent is False
    bot.send_chat_action.assert_not_called()


@pytest.mark.asyncio
async def test_call_after_window_fires_again(monkeypatch) -> None:
    bot = AsyncMock()
    fake_time = [1000.0]
    monkeypatch.setattr(typing_mod.time, "monotonic", lambda: fake_time[0])
    await fire_typing(bot, 42, "test")
    fake_time[0] += TYPING_REFRESH_INTERVAL + 0.01
    sent = await fire_typing(bot, 42, "test")
    assert sent is True
    assert bot.send_chat_action.await_count == 2


@pytest.mark.asyncio
async def test_separate_users_are_throttled_independently() -> None:
    bot = AsyncMock()
    sent_a = await fire_typing(bot, 1, "test")
    sent_b = await fire_typing(bot, 2, "test")
    assert sent_a is True
    assert sent_b is True
    assert bot.send_chat_action.await_count == 2


@pytest.mark.asyncio
async def test_api_failure_does_not_lock_throttle(monkeypatch) -> None:
    """A failed send_chat_action must not stamp the cache —
    otherwise a transient TG hiccup at t=0 would suppress every
    later call until TYPING_REFRESH_INTERVAL expires."""
    bot = AsyncMock()
    bot.send_chat_action.side_effect = RuntimeError("network")
    sent = await fire_typing(bot, 42, "test")
    assert sent is False
    # Cache must be empty so the next call can retry.
    assert 42 not in typing_mod._last_fired

    bot.send_chat_action.side_effect = None
    sent = await fire_typing(bot, 42, "test")
    assert sent is True


@pytest.mark.asyncio
async def test_high_rate_burst_collapses_to_one_call(monkeypatch) -> None:
    """20 polls within 1 s — the original spam pattern from
    status_polling. Exactly one chat-action should reach Telegram."""
    bot = AsyncMock()
    fake_time = [1000.0]
    monkeypatch.setattr(typing_mod.time, "monotonic", lambda: fake_time[0])
    for _ in range(20):
        await fire_typing(bot, 42, "status_polling")
        fake_time[0] += 0.05  # 50 ms between polls
    assert bot.send_chat_action.await_count == 1
