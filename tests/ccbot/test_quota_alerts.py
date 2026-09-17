"""Tests for handlers.quota_alerts level transitions."""

from unittest.mock import AsyncMock

import pytest

from ccbot.codex_usage import CodexRateLimitWindow, CodexUsageInfo
from ccbot.handlers import quota_alerts
from ccbot.handlers.quota_alerts import _level_for_pct


class TestLevelForPct:
    def test_below_first_threshold(self) -> None:
        assert _level_for_pct(0) == 0
        assert _level_for_pct(49) == 0

    def test_at_first_threshold(self) -> None:
        assert _level_for_pct(50) == 1
        assert _level_for_pct(74) == 1

    def test_at_second_threshold(self) -> None:
        assert _level_for_pct(75) == 2
        assert _level_for_pct(89) == 2

    def test_at_third_threshold(self) -> None:
        assert _level_for_pct(90) == 3
        assert _level_for_pct(100) == 3

    def test_levels_are_monotonic(self) -> None:
        levels = [_level_for_pct(p) for p in range(0, 101)]
        assert all(b >= a for a, b in zip(levels, levels[1:]))


@pytest.mark.asyncio
async def test_interval_poll_refreshes_selected_codex_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = CodexUsageInfo(
        weekly=CodexRateLimitWindow(
            used_percent=18,
            duration_minutes=10_080,
            resets_at=1_900_000_000,
        )
    )
    fetch = AsyncMock(return_value=info)
    monkeypatch.setattr("ccbot.bot._usage_window.fetch_live_usage", fetch)

    await quota_alerts._poll_once(AsyncMock(), suppress_push=True)

    fetch.assert_awaited_once_with()
