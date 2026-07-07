"""Tests for the liveness watchdog — the safety net for a wedged event loop
(or a getUpdates read that never raises) that the NetworkError/Conflict
counters in ``_error_handler`` cannot see, since those only fire on an
actual exception.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from ccbot.bot import app


@pytest.fixture(autouse=True)
def _reset_heartbeat() -> Iterator[None]:
    before = app._last_heartbeat
    yield
    app._last_heartbeat = before


def test_fresh_heartbeat_does_not_terminate(monkeypatch: pytest.MonkeyPatch):
    terminate = MagicMock()
    monkeypatch.setattr(app, "_terminate_for_sustained_conflict", terminate)

    clock = {"t": 1000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])

    app._last_heartbeat = clock["t"]
    app._liveness_watchdog_tick()

    terminate.assert_not_called()


def test_stale_heartbeat_terminates(monkeypatch: pytest.MonkeyPatch):
    terminate = MagicMock()
    monkeypatch.setattr(app, "_terminate_for_sustained_conflict", terminate)

    clock = {"t": 2000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])

    app._last_heartbeat = clock["t"]
    clock["t"] += app.LIVENESS_MAX_STALE_SECONDS + 1.0
    app._liveness_watchdog_tick()

    terminate.assert_called_once()


def test_heartbeat_within_budget_does_not_terminate(monkeypatch: pytest.MonkeyPatch):
    """Just under the stale threshold is still tolerated — no exit."""
    terminate = MagicMock()
    monkeypatch.setattr(app, "_terminate_for_sustained_conflict", terminate)

    clock = {"t": 3000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])

    app._last_heartbeat = clock["t"]
    clock["t"] += app.LIVENESS_MAX_STALE_SECONDS - 1.0
    app._liveness_watchdog_tick()

    terminate.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_loop_ticks(monkeypatch: pytest.MonkeyPatch):
    """The async loop actually advances ``_last_heartbeat`` on each tick."""
    import asyncio

    clock = {"t": 4000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds
        if len(sleeps) >= 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(app.asyncio, "sleep", fake_sleep)

    app._last_heartbeat = 0.0
    with pytest.raises(asyncio.CancelledError):
        await app._heartbeat_loop()

    # The heartbeat is set BEFORE each sleep, so after the 3rd sleep raises,
    # the timestamp reflects the tick that preceded it (2 sleeps' worth).
    assert app._last_heartbeat == 4000.0 + 2 * app.LIVENESS_TICK_SECONDS
    assert sleeps == [app.LIVENESS_TICK_SECONDS] * 3
