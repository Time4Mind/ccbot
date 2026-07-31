"""Codex app-server rate-limit parsing and rendering."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ccbot import codex_usage
from ccbot.codex_usage import (
    parse_rate_limits_result,
    parse_rollout_rate_limits,
    read_latest_rollout_usage,
)
from ccbot.usage import _daily_quota_budget, format_usage_breakdown_compact


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
    assert "Used: 61%" in rendered
    assert "Remaining:" not in rendered
    assert "Today:" in rendered
    assert "Reset:" in rendered
    assert " · " not in rendered.split("week", 1)[1]
    assert "\n\nUsed: 61%" in rendered


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


def test_parses_snake_case_rollout_rate_limits() -> None:
    info = parse_rollout_rate_limits(
        {
            "limit_id": "codex",
            "primary": {
                "used_percent": 11.0,
                "window_minutes": 10_080,
                "resets_at": 1_786_028_677,
            },
            "secondary": None,
        }
    )

    assert info is not None
    assert info.five_hour is None
    assert info.weekly is not None
    assert info.weekly.used_percent == 11
    assert info.weekly.resets_at == 1_786_028_677


def test_reads_latest_usage_from_recent_rollout(tmp_path: Path) -> None:
    older = tmp_path / "2026" / "07" / "30" / "rollout-old.jsonl"
    newer = tmp_path / "2026" / "07" / "31" / "rollout-new.jsonl"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)

    def token_event(used_percent: int) -> str:
        return json.dumps(
            {
                "timestamp": "2026-07-31T12:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": used_percent,
                            "window_minutes": 10_080,
                            "resets_at": 1_786_028_677,
                        },
                        "secondary": None,
                    },
                },
            }
        )

    older.write_text(token_event(7) + "\n")
    newer.write_text(token_event(11) + "\n")

    info = read_latest_rollout_usage(tmp_path)

    assert info is not None
    assert info.weekly is not None
    assert info.weekly.used_percent == 11


@pytest.mark.asyncio
async def test_fetch_falls_back_to_rollout_when_app_server_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rollout = tmp_path / "2026" / "07" / "31" / "rollout-live.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 12,
                            "window_minutes": 10_080,
                            "resets_at": 1_786_028_677,
                        }
                    },
                },
            }
        )
        + "\n"
    )

    async def app_server_fails(*_args: object, **_kwargs: object) -> None:
        raise OSError("account unavailable")

    monkeypatch.setattr(codex_usage.config, "codex_sessions_path", tmp_path)
    monkeypatch.setattr(codex_usage.asyncio, "create_subprocess_exec", app_server_fails)

    info = await codex_usage.fetch_codex_usage()

    assert info is not None
    assert info.weekly is not None
    assert info.weekly.used_percent == 12


def test_daily_budget_uses_equal_calendar_day_buckets() -> None:
    now = datetime(2026, 7, 31, 18, 0)
    reset = now + timedelta(days=3, hours=6)

    result = _daily_quota_budget(50, int(reset.timestamp()), None, now=now)

    assert result is not None
    today_left, state = result
    assert today_left == 10.0
    assert state["daily_budget"] == 10.0


def test_daily_budget_stays_fixed_and_reports_overspend() -> None:
    now = datetime(2026, 7, 31, 18, 0)
    reset = now + timedelta(days=3, hours=6)
    initial = _daily_quota_budget(50, int(reset.timestamp()), None, now=now)
    assert initial is not None

    result = _daily_quota_budget(64, int(reset.timestamp()), initial[1], now=now)

    assert result is not None
    assert result[0] == -4.0


def test_overspend_is_redistributed_on_the_next_day() -> None:
    now = datetime(2026, 7, 31, 18, 0)
    reset = datetime(2026, 8, 4, 20, 0)
    initial = _daily_quota_budget(50, int(reset.timestamp()), None, now=now)
    assert initial is not None

    tomorrow = datetime(2026, 8, 1, 9, 0)
    result = _daily_quota_budget(64, int(reset.timestamp()), initial[1], now=tomorrow)

    assert result is not None
    today_left, state = result
    assert today_left == 9.0
    assert state["daily_budget"] == 9.0
