"""Explicit acknowledgement reconciliation never bypasses ownership or flood waits."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.commands.delivery import delivery_callback, delivery_command


def update_for(user_id=12345, data=""):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(reply_text=AsyncMock()),
        callback_query=SimpleNamespace(
            data=data, answer=AsyncMock(), edit_message_reply_markup=AsyncMock()
        ),
    )


@pytest.mark.asyncio
async def test_retry_requires_explicit_owner_and_retains_cooldown(
    isolated_final_deliveries,
):
    q = isolated_final_deliveries
    item = q.enqueue(12345, "session", {"text": "answer"})
    item.status = "unknown"
    item.cooldown_until = 10**12
    q.save()
    foreign = update_for(42, f"delivery:retry:{item.id}")
    await delivery_callback(foreign, SimpleNamespace())
    assert item.status == "unknown"
    foreign.callback_query.answer.assert_not_awaited()
    owner = update_for(data=f"delivery:retry:{item.id}")
    await delivery_callback(owner, SimpleNamespace())
    assert item.status == "pending"
    assert item.cooldown_until == 10**12
    driver = AsyncMock(return_value=True)
    assert not await q.attempt(item, driver)
    driver.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_received_removes_ambiguous_obligation(
    isolated_final_deliveries,
):
    q = isolated_final_deliveries
    item = q.enqueue(12345, "session", {"text": "answer"})
    item.status = "unknown"
    update = update_for(data=f"delivery:ack:{item.id}")
    await delivery_callback(update, SimpleNamespace())
    assert not q.pending(12345)
    update.callback_query.edit_message_reply_markup.assert_awaited_once_with(
        reply_markup=None
    )


@pytest.mark.asyncio
async def test_pending_status_warns_before_explicit_replay(
    isolated_final_deliveries, monkeypatch
):
    from ccbot.bot.commands import delivery

    q = isolated_final_deliveries
    item = q.enqueue(12345, "session", {"text": "answer"})
    item.status = "unknown"
    reply = AsyncMock()
    monkeypatch.setattr(delivery, "safe_reply", reply)
    await delivery_command(update_for(), SimpleNamespace())
    assert "Проверьте чат перед повтором" in reply.call_args.args[1]
    keyboard = reply.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][1].callback_data == f"delivery:retry:{item.id}"
