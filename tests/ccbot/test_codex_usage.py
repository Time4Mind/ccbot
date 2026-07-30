"""Codex app-server rate-limit parsing and rendering."""

from __future__ import annotations

from ccbot.codex_usage import parse_rate_limits_result
from ccbot.usage import format_usage_breakdown_compact


def test_parses_five_hour_and_weekly_windows() -> None:
    info = parse_rate_limits_result(
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 23.4,
                    "windowDurationMins": 300,
                    "resetsAt": 1_800_000_000,
                },
                "secondary": {
                    "usedPercent": 61,
                    "windowDurationMins": 10_080,
                    "resetsAt": 1_800_500_000,
                },
            }
        }
    )

    assert info is not None
    assert info.five_hour is not None
    assert info.five_hour.used_percent == 23
    assert info.weekly is not None
    assert info.weekly.used_percent == 61

    rendered = format_usage_breakdown_compact(1, info)
    assert rendered is not None
    assert "*OpenAI Codex*" in rendered
    assert "23%" in rendered
    assert "61%" in rendered


def test_parses_weekly_only_response() -> None:
    info = parse_rate_limits_result(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 41,
                        "windowDurationMins": 10_080,
                        "resetsAt": None,
                    },
                    "secondary": None,
                }
            }
        }
    )

    assert info is not None
    assert info.five_hour is None
    assert info.weekly is not None
    assert info.weekly.used_percent == 41
    rendered = format_usage_breakdown_compact(1, info)
    assert rendered is not None
    assert "5h: not reported by Codex" in rendered
    assert "41%" in rendered


def test_rejects_missing_windows() -> None:
    assert parse_rate_limits_result({"rateLimits": {}}) is None
