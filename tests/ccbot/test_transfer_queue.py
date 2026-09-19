from __future__ import annotations

from types import SimpleNamespace

import pytest

from ccbot.transfer_queue import (
    begin_transfer_queue,
    bind_transfer_queue,
    capture_transfer_message,
    pending_transfer_count,
    reset_transfer_queues_for_test,
)


@pytest.fixture(autouse=True)
def _reset_queue():
    reset_transfer_queues_for_test()
    yield
    reset_transfer_queues_for_test()


def _update(text: str, message_id: int = 1):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(text=text, message_id=message_id),
    )


def test_transfer_queue_captures_prompts_but_leaves_control_commands():
    begin_transfer_queue(42, "transfer1")
    context = SimpleNamespace()

    assert capture_transfer_message(_update("первый запрос"), context)
    assert capture_transfer_message(_update("второй запрос", 2), context)
    assert not capture_transfer_message(_update("/menu", 3), context)
    assert pending_transfer_count(42) == 2


@pytest.mark.asyncio
async def test_transfer_queue_drains_in_fifo_order_after_target_ready():
    begin_transfer_queue(42, "transfer1")
    context = SimpleNamespace()
    capture_transfer_message(_update("first"), context)
    capture_transfer_message(_update("second", 2), context)
    delivered: list[str] = []

    async def deliver(update, _context):
        delivered.append(update.message.text)
        return True

    task = bind_transfer_queue(42, deliver)
    assert task is not None
    await task

    assert delivered == ["first", "second"]
    assert pending_transfer_count(42) == 0
