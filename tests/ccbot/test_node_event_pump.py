from __future__ import annotations

import asyncio

import pytest

from ccbot.node_event_pump import NodeEventPump
from ccbot.node_transport import NodeEnvelope


class EventTransport:
    def __init__(self) -> None:
        self.sent: list[NodeEnvelope] = []
        self.delivered = asyncio.Event()

    async def send(self, message: NodeEnvelope) -> None:
        self.sent.append(message)
        self.delivered.set()

    async def receive(self) -> NodeEnvelope:
        raise AssertionError("receive is not used")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_unacknowledged_event_survives_worker_restart(tmp_path) -> None:
    state_path = tmp_path / "events.json"
    first_transport = EventTransport()
    first = NodeEventPump("worker-a", state_path=state_path, poll_interval=0.01)
    polls = 0

    async def poll_events():
        nonlocal polls
        polls += 1
        return [
            {
                "event_type": "session_message",
                "session_id": "session-1",
                "text": "answer",
            }
        ]

    first_run = asyncio.create_task(
        first.run(
            poll_events,
            transport=lambda: first_transport,
            publish_health=lambda: asyncio.sleep(0),
        )
    )
    await asyncio.wait_for(first_transport.delivered.wait(), timeout=1)
    first_run.cancel()
    await asyncio.gather(first_run, return_exceptions=True)

    second_transport = EventTransport()
    second = NodeEventPump("worker-a", state_path=state_path, poll_interval=0.01)
    second_run = asyncio.create_task(
        second.run(
            lambda: asyncio.sleep(0, result=[]),
            transport=lambda: second_transport,
            publish_health=lambda: asyncio.sleep(0),
        )
    )
    await asyncio.wait_for(second_transport.delivered.wait(), timeout=1)
    replay = second_transport.sent[0]
    second.acknowledge(replay.request_id)
    second_run.cancel()
    await asyncio.gather(second_run, return_exceptions=True)

    assert polls == 1
    assert replay.kind == "event"
    assert replay.request_id == first_transport.sent[0].request_id
    assert replay.sequence == first_transport.sent[0].sequence == 1
    assert replay.payload["text"] == "answer"
    assert not second.pending
