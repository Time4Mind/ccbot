from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter

from ccbot.telegram_rate_limit import (
    PersistentEndpointRateLimiter,
    background_telegram_request,
)


async def _request(
    limiter: PersistentEndpointRateLimiter,
    callback: AsyncMock,
    *,
    endpoint: str = "editMessageText",
    chat_id: int = 42,
    message_id: int = 9,
):
    return await limiter.process_request(
        callback,
        (),
        {},
        endpoint,
        {"chat_id": chat_id, "message_id": message_id},
        None,
    )


@pytest.mark.asyncio
async def test_retry_after_persists_and_only_blocks_the_offending_operation(
    tmp_path,
) -> None:
    state_file = tmp_path / "telegram-rate-limits.json"
    limiter = PersistentEndpointRateLimiter(token="token-a", cooldown_path=state_file)
    limited = AsyncMock(side_effect=RetryAfter(120))

    with background_telegram_request(), pytest.raises(RetryAfter):
        await _request(limiter, limited)

    assert limited.await_count == 1
    stored = json.loads(state_file.read_text())
    assert len(stored["cooldowns"]) == 1

    same_operation = AsyncMock(return_value=True)
    with background_telegram_request(), pytest.raises(RetryAfter):
        await _request(limiter, same_operation)
    same_operation.assert_not_awaited()

    other_message = AsyncMock(return_value=True)
    with background_telegram_request():
        assert await _request(limiter, other_message, message_id=10) is True
    other_message.assert_awaited_once()

    other_endpoint = AsyncMock(return_value=True)
    with background_telegram_request():
        assert (
            await _request(limiter, other_endpoint, endpoint="answerCallbackQuery")
            is True
        )
    other_endpoint.assert_awaited_once()

    # An explicit user action is never held behind a background cooldown.
    foreground = AsyncMock(return_value=True)
    assert await _request(limiter, foreground) is True
    foreground.assert_awaited_once()


@pytest.mark.asyncio
async def test_cooldown_survives_restart_but_is_scoped_to_token(tmp_path) -> None:
    state_file = tmp_path / "telegram-rate-limits.json"
    first = PersistentEndpointRateLimiter(token="token-a", cooldown_path=state_file)
    with background_telegram_request(), pytest.raises(RetryAfter):
        await _request(first, AsyncMock(side_effect=RetryAfter(120)))

    restarted = PersistentEndpointRateLimiter(token="token-a", cooldown_path=state_file)
    blocked = AsyncMock(return_value=True)
    with background_telegram_request(), pytest.raises(RetryAfter):
        await _request(restarted, blocked)
    blocked.assert_not_awaited()

    replacement_token = PersistentEndpointRateLimiter(
        token="token-b", cooldown_path=state_file
    )
    allowed = AsyncMock(return_value=True)
    with background_telegram_request():
        assert await _request(replacement_token, allowed) is True
    allowed.assert_awaited_once()


@pytest.mark.asyncio
async def test_transport_logs_endpoint_outcome_and_latency_without_body(
    tmp_path, caplog
):
    limiter = PersistentEndpointRateLimiter(
        token="secret-token", cooldown_path=tmp_path / "limit.json"
    )
    caplog.set_level("INFO")
    await limiter.process_request(
        AsyncMock(return_value=True),
        (),
        {},
        "sendMessage",
        {"chat_id": 42, "text": "private message"},
        None,
    )
    assert "telegram_request" in caplog.text
    assert "endpoint=sendMessage" in caplog.text
    assert "outcome=ok" in caplog.text
    assert "elapsed_ms=" in caplog.text
    assert "private message" not in caplog.text
    assert "secret-token" not in caplog.text
