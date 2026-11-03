"""An explicit delivery action remains available for a saved uncertain answer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from ccbot.bot.commands import delivery


@pytest.mark.parametrize("action", ["ack", "retry"])
async def test_explicit_action_can_settle_a_history_only_unknown_answer(
    isolated_final_deliveries, monkeypatch, action
):
    queue = isolated_final_deliveries
    item = queue.enqueue(42, "session", {"text": "saved answer"})
    item.status = "unknown"
    queue.defer_to_history(42, "session")
    monkeypatch.setattr(delivery, "is_user_allowed", lambda _: True)
    query = SimpleNamespace(
        data=f"delivery:{action}:{item.id}",
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42), callback_query=query
    )
    await delivery.delivery_callback(update, SimpleNamespace())
    driver = AsyncMock(return_value=True)
    if action == "ack":
        assert not queue.records
        driver.assert_not_awaited()
    else:
        assert await queue.attempt(item, driver)
        driver.assert_awaited_once()
        assert not queue.records
