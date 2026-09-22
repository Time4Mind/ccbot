from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ccbot.telegram_polling_health import (
    PollingHeartbeatRequest,
    PollingHealth,
    probe_health_file,
)


class FakeRequest:
    read_timeout = 5.0

    def __init__(self, result: tuple[int, bytes] = (200, b'{"ok":true,"result":[]}')):
        self.result = result
        self.initialized = False
        self.closed = False

    async def initialize(self) -> None:
        self.initialized = True

    async def shutdown(self) -> None:
        self.closed = True

    async def do_request(self, **_kwargs):
        return self.result


def _health(tmp_path, clock, wall) -> PollingHealth:
    return PollingHealth(
        stale_seconds=180,
        startup_grace_seconds=120,
        state_path=tmp_path / "poll-health.json",
        clock=lambda: clock["now"],
        wall_clock=lambda: wall["now"],
    )


@pytest.mark.asyncio
async def test_successful_empty_poll_refreshes_heartbeat_and_atomic_health_file(
    tmp_path,
) -> None:
    clock = {"now": 1000.0}
    wall = {"now": 10_000.0}
    health = _health(tmp_path, clock, wall)
    health.start()
    success = MagicMock()
    request = PollingHeartbeatRequest(  # type: ignore[arg-type]
        FakeRequest(), health, on_success=success
    )

    result = await request.do_request(url="https://telegram/getUpdates", method="POST")
    await request.initialize()
    await request.shutdown()

    assert result == (200, b'{"ok":true,"result":[]}')
    assert health.status() == ("healthy", 0.0)
    payload = json.loads(health.state_path.read_text(encoding="utf-8"))
    assert payload["status"] == "healthy"
    assert payload["last_poll_at"] == wall["now"]
    assert set(payload) == {
        "last_poll_at",
        "pid",
        "stale_after_seconds",
        "started_at",
        "startup_grace_seconds",
        "status",
    }
    assert list(tmp_path.glob("*.tmp")) == []
    success.assert_called_once_with()
    assert request.delegate.initialized is True
    assert request.delegate.closed is True


@pytest.mark.asyncio
async def test_failed_poll_does_not_refresh_heartbeat(tmp_path) -> None:
    clock = {"now": 1000.0}
    wall = {"now": 10_000.0}
    health = _health(tmp_path, clock, wall)
    health.start()
    request = PollingHeartbeatRequest(  # type: ignore[arg-type]
        FakeRequest((502, b'{"ok":false}')), health
    )

    await request.do_request(url="https://telegram/getUpdates", method="POST")

    assert health.last_success_at == 0.0
    assert health.status()[0] == "starting"


@pytest.mark.asyncio
async def test_http_200_bot_api_error_does_not_refresh_heartbeat(tmp_path) -> None:
    clock = {"now": 1000.0}
    wall = {"now": 10_000.0}
    health = _health(tmp_path, clock, wall)
    health.start()
    request = PollingHeartbeatRequest(  # type: ignore[arg-type]
        FakeRequest((200, b'{"ok":false,"description":"failed"}')), health
    )

    await request.do_request(url="https://telegram/getUpdates", method="POST")

    assert health.last_success_at == 0.0


def test_startup_grace_and_poll_staleness_use_independent_clock(tmp_path) -> None:
    clock = {"now": 1000.0}
    wall = {"now": 10_000.0}
    health = _health(tmp_path, clock, wall)
    health.start()

    clock["now"] += 119
    assert health.status()[0] == "starting"
    clock["now"] += 2
    assert health.status()[0] == "stale"

    health.record_success()
    clock["now"] += 179
    assert health.status()[0] == "healthy"
    clock["now"] += 2
    assert health.status()[0] == "stale"


def test_graceful_shutdown_disarms_polling_failure(tmp_path) -> None:
    clock = {"now": 1000.0}
    wall = {"now": 10_000.0}
    health = _health(tmp_path, clock, wall)
    health.start()
    health.mark_stopping()
    clock["now"] += 1000

    assert health.status()[0] == "stopping"
    assert health.claim_terminal() is False


def test_health_file_probe_transitions_from_healthy_to_stale(tmp_path) -> None:
    clock = {"now": 1000.0}
    wall = {"now": 10_000.0}
    health = _health(tmp_path, clock, wall)
    health.start()
    assert probe_health_file(health.state_path, now=10_100.0)

    health.record_success()
    assert probe_health_file(health.state_path, now=10_179.0)
    assert not probe_health_file(health.state_path, now=10_181.0)
