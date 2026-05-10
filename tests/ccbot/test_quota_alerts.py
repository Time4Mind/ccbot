"""Tests for handlers.quota_alerts level transitions."""

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
