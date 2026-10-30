import logging
from unittest.mock import AsyncMock

import pytest

from ccbot.bot import _app_lifecycle


@pytest.mark.asyncio
async def test_relay_aggregates_repeats_and_reports_new_error_and_recovery(
    monkeypatch, caplog
):
    connect = AsyncMock(
        side_effect=[
            OSError("offline"),
            OSError("offline"),
            ValueError("different"),
            None,
        ]
    )
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(_app_lifecycle.asyncio, "sleep", sleep)
    with caplog.at_level(logging.INFO):
        await _app_lifecycle._connect_relay_with_backoff(connect)
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "offline" in warnings[0] and "different" in warnings[1]
    assert any("recovered after 3 attempts" in r.message for r in caplog.records)
    assert delays == [5.0, 10.0, 20.0]
