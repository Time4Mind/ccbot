"""Durable delivery retry, crash recovery, ordering and acknowledgement contracts."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import NetworkError, RetryAfter, TimedOut

from ccbot import final_delivery as delivery


def offline():
    error = NetworkError("connection failed")
    error.__cause__ = httpx.ConnectError("offline")
    return error


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    waits = []

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay
        await asyncio.sleep(0)

    monkeypatch.setattr(delivery, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(
        delivery,
        "asyncio",
        SimpleNamespace(
            Lock=asyncio.Lock,
            Task=asyncio.Task,
            CancelledError=asyncio.CancelledError,
            create_task=asyncio.create_task,
            gather=asyncio.gather,
            sleep=sleep,
        ),
    )
    return now, waits


@pytest.mark.asyncio
async def test_long_outage_recovers_with_capped_delays_after_each_attempt(
    isolated_final_deliveries, clock
):
    queue = isolated_final_deliveries
    now, waits = clock
    queue.enqueue(42, "session", {"text": "answer"})
    attempts = []
    done = asyncio.Event()

    async def send(item):
        attempts.append(now[0])
        now[0] += 3.0  # request duration must not consume the backoff
        if len(attempts) <= 8:
            raise offline()
        done.set()
        return True

    queue.start(send)
    await asyncio.wait_for(done.wait(), 1)
    assert waits == [1, 2, 4, 8, 15, 15, 15, 15]
    assert [b - a - 3 for a, b in zip(attempts, attempts[1:])] == waits
    assert not queue.pending(42)
    assert not delivery.FinalDeliveryQueue(queue.path).pending(42)


@pytest.mark.asyncio
async def test_restart_retains_answer_and_retry_deadline(
    isolated_final_deliveries, clock
):
    queue = isolated_final_deliveries
    item = queue.enqueue(42, "session", {"text": "original answer"})
    await queue.attempt(item, AsyncMock(side_effect=offline()))
    restarted = delivery.FinalDeliveryQueue(queue.path)
    recovered = restarted.pending(42)[0]
    assert recovered.payload["text"] == "original answer"
    assert recovered.retry_at == 1001
    driver = AsyncMock(return_value=True)
    assert not await restarted.attempt(recovered, driver)
    driver.assert_not_awaited()
    clock[0][0] = 1001
    assert await restarted.attempt(recovered, driver)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [90, timedelta(seconds=90)])
async def test_retry_after_survives_restart_and_cannot_be_accelerated(
    isolated_final_deliveries, clock, wait
):
    queue = isolated_final_deliveries
    item = queue.enqueue(42, "session", {"text": "answer"})
    await queue.attempt(item, AsyncMock(side_effect=RetryAfter(wait)))
    queue = delivery.FinalDeliveryQueue(queue.path)
    item = queue.pending(42)[0]
    assert item.cooldown_until == 1090
    driver = AsyncMock(return_value=True)
    clock[0][0] = 1089
    item.retry_at = 0  # even an ordinary retry acceleration cannot bypass flood wait
    assert not await queue.attempt(item, driver)
    driver.assert_not_awaited()
    clock[0][0] = 1090
    assert await queue.attempt(item, driver)


@pytest.mark.asyncio
async def test_unknown_send_is_not_repeated_and_holds_later_turns(
    isolated_final_deliveries, clock
):
    queue = isolated_final_deliveries
    first = queue.enqueue(42, "session", {"text": "answer A"})
    second = queue.enqueue(42, "session", {"text": "answer B"})
    await queue.attempt(first, AsyncMock(side_effect=TimedOut()))
    assert first.status == "unknown"
    driver = AsyncMock(return_value=True)
    clock[0][0] += 100
    assert not await queue.attempt(first, driver)
    assert not await queue.attempt(second, driver)
    driver.assert_not_awaited()
    restarted = delivery.FinalDeliveryQueue(queue.path)
    assert [r.payload["text"] for r in restarted.pending(42)] == [
        "answer A",
        "answer B",
    ]
    assert restarted.pending(42)[0].status == "unknown"
    queue.resolve(first)  # explicit user acknowledgement
    assert await queue.attempt(second, driver)


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id,expected", [(None, "unknown"), (9, "pending")])
async def test_crash_during_request_distinguishes_send_from_idempotent_edit(
    isolated_final_deliveries, message_id, expected
):
    queue = isolated_final_deliveries
    item = queue.enqueue(42, "session", {"text": "answer"})
    if message_id:
        queue.bind_ack(item.id, message_id)
    started = asyncio.Event()

    async def in_flight(_item):
        started.set()
        await asyncio.Event().wait()
        return True

    task = asyncio.create_task(queue.attempt(item, in_flight))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    recovered = delivery.FinalDeliveryQueue(queue.path).pending(42)[0]
    assert recovered.status == expected
    assert recovered.message_id == message_id


@pytest.mark.asyncio
async def test_lost_edit_acknowledgement_retries_same_identity(
    isolated_final_deliveries, clock
):
    queue = isolated_final_deliveries
    item = queue.enqueue(42, "session", {"text": "answer"})
    queue.bind_ack(item.id, 9)
    seen = []

    async def edit(item):
        seen.append(item.message_id)
        if len(seen) == 1:
            raise TimedOut()
        return True

    assert not await queue.attempt(item, edit)
    assert item.status == "pending"
    clock[0][0] += 1
    assert await queue.attempt(item, edit)
    assert seen == [9, 9]


@pytest.mark.asyncio
async def test_fifo_delivers_both_answers_after_recovery(
    isolated_final_deliveries, clock
):
    queue = isolated_final_deliveries
    first = queue.enqueue(42, "session", {"text": "answer A"})
    second = queue.enqueue(42, "session", {"text": "answer B"})
    await queue.attempt(first, AsyncMock(side_effect=offline()))
    delivered = []

    async def send(item):
        delivered.append(item.payload["text"])
        return True

    assert not await queue.attempt(second, send)
    clock[0][0] += 1
    assert await queue.attempt(first, send)
    assert await queue.attempt(second, send)
    assert delivered == ["answer A", "answer B"]


def test_error_message_is_not_proof_of_presubmission():
    assert not delivery.before_submission(NetworkError("httpx.ConnectError: simulated"))
    assert delivery.before_submission(offline())


@pytest.mark.asyncio
async def test_replayed_event_is_deduplicated_while_pending_and_after_receipt(
    isolated_final_deliveries,
):
    q = isolated_final_deliveries
    first = q.enqueue(42, "session", {"text": "same"}, event_key="turn-one")
    again = q.enqueue(42, "session", {"text": "same"}, event_key="turn-one")
    assert first is again
    assert len(q.pending(42)) == 1
    q.resolve(first)
    restarted = delivery.FinalDeliveryQueue(q.path)
    assert (
        restarted.enqueue(42, "session", {"text": "same"}, event_key="turn-one") is None
    )
    # Identical text on another real turn remains a distinct obligation.
    assert (
        restarted.enqueue(42, "session", {"text": "same"}, event_key="turn-two")
        is not None
    )
