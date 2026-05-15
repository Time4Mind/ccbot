"""Tests for usage.parse_session_usage and aggregate_session."""

import json
from pathlib import Path

import pytest

from ccbot.session import SessionManager
from ccbot.usage import parse_session_usage


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
