"""Tests for usage.parse_session_usage, aggregate_session, and per-session
token alerts."""

import json
from pathlib import Path

import pytest

from ccbot.session import Session, SessionManager
from ccbot.usage import parse_session_usage, pop_session_token_alert


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    return SessionManager()


def _write_jsonl(p: Path, lines: list[dict]) -> None:
    p.write_text("".join(json.dumps(x) + "\n" for x in lines))


class TestParseSessionUsage:
    @pytest.mark.asyncio
    async def test_extracts_usage_from_assistant_turns(self, tmp_path: Path) -> None:
        f = tmp_path / "s.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-05-08T10:00:00Z",
                    "message": {"usage": {"input_tokens": 100, "output_tokens": 50}},
                },
                {
                    "type": "user",
                    "timestamp": "2026-05-08T10:00:01Z",
                    "message": {"content": "hi"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-05-08T10:01:00Z",
                    "message": {"usage": {"input_tokens": 200, "output_tokens": 25}},
                },
            ],
        )
        turns = await parse_session_usage(f)
        assert len(turns) == 2
        assert turns[0].input_tokens == 100
        assert turns[0].output_tokens == 50
        assert turns[1].total == 225

    @pytest.mark.asyncio
    async def test_skips_zero_tokens(self, tmp_path: Path) -> None:
        f = tmp_path / "s.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-05-08T10:00:00Z",
                    "message": {"usage": {"input_tokens": 0, "output_tokens": 0}},
                }
            ],
        )
        turns = await parse_session_usage(f)
        assert turns == []

    @pytest.mark.asyncio
    async def test_handles_malformed_json_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "s.jsonl"
        f.write_text(
            "not valid json\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-05-08T10:00:00Z",
                    "message": {"usage": {"input_tokens": 10, "output_tokens": 5}},
                }
            )
            + "\n"
        )
        turns = await parse_session_usage(f)
        assert len(turns) == 1
        assert turns[0].total == 15

    @pytest.mark.asyncio
    async def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        turns = await parse_session_usage(tmp_path / "nope.jsonl")
        assert turns == []


class TestSessionTokenAlert:
    def _user_settings_returns(self, monkeypatch, settings: dict) -> None:
        from ccbot.session import session_manager

        monkeypatch.setattr(session_manager, "get_user_settings", lambda uid: settings)

    def test_below_lowest_threshold_silent(self, monkeypatch) -> None:
        self._user_settings_returns(
            monkeypatch, {"session_token_alerts": [100_000, 200_000, 400_000]}
        )
        sess = Session(id="abc", name="x", token_usage_total=50_000)
        assert pop_session_token_alert(sess, 1) is None
        assert sess.alerted_token_thresholds == []

    def test_crosses_first_threshold_then_suppressed(self, monkeypatch) -> None:
        self._user_settings_returns(
            monkeypatch, {"session_token_alerts": [100_000, 200_000, 400_000]}
        )
        sess = Session(id="abc", name="x", token_usage_total=120_000)
        assert pop_session_token_alert(sess, 1) == 100_000
        assert sess.alerted_token_thresholds == [100_000]
        # Same session, same total — don't re-fire the same threshold.
        assert pop_session_token_alert(sess, 1) is None

    def test_crosses_two_thresholds_in_separate_calls(self, monkeypatch) -> None:
        self._user_settings_returns(
            monkeypatch, {"session_token_alerts": [100_000, 200_000, 400_000]}
        )
        sess = Session(id="abc", name="x", token_usage_total=120_000)
        assert pop_session_token_alert(sess, 1) == 100_000
        sess.token_usage_total = 250_000
        assert pop_session_token_alert(sess, 1) == 200_000
        assert sess.alerted_token_thresholds == [100_000, 200_000]

    def test_uses_default_when_setting_missing(self, monkeypatch) -> None:
        from ccbot.config import config

        self._user_settings_returns(monkeypatch, {})
        sess = Session(
            id="abc",
            name="x",
            token_usage_total=config.session_token_alert_defaults[0],
        )
        assert (
            pop_session_token_alert(sess, 1) == (config.session_token_alert_defaults[0])
        )
