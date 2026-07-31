"""Tests for usage.parse_session_usage and aggregate_session."""

import json
from pathlib import Path

import pytest

from ccbot.session import Session, SessionManager
from ccbot.usage import context_pct_for_session, parse_session_usage


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


class TestCodexContextUsage:
    @pytest.mark.asyncio
    async def test_uses_latest_exact_token_count_and_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rollout = tmp_path / "rollout.jsonl"
        _write_jsonl(
            rollout,
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"total_tokens": 25_000},
                            "model_context_window": 250_000,
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"total_tokens": 64_600},
                            "model_context_window": 258_400,
                        },
                    },
                },
            ],
        )
        sess = Session(
            id="codex1",
            name="codex",
            workdir=str(tmp_path),
            claude_session_id="codex-session-id",
            backend="codex",
        )
        monkeypatch.setattr(
            "ccbot.codex_session_io.build_session_file_path",
            lambda session_id, cwd="": rollout,
        )

        assert await context_pct_for_session(sess) == 25

    @pytest.mark.asyncio
    async def test_compaction_uses_latest_non_cumulative_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rollout = tmp_path / "rollout.jsonl"
        _write_jsonl(
            rollout,
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 2_000_000},
                            "last_token_usage": {"total_tokens": 200_000},
                            "model_context_window": 250_000,
                        },
                    },
                },
                {"type": "event_msg", "payload": {"type": "context_compacted"}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 2_050_000},
                            "last_token_usage": {"total_tokens": 50_000},
                            "model_context_window": 250_000,
                        },
                    },
                },
            ],
        )
        sess = Session(
            id="codex2",
            name="codex",
            workdir=str(tmp_path),
            claude_session_id="codex-session-id",
            backend="codex",
        )
        monkeypatch.setattr(
            "ccbot.codex_session_io.build_session_file_path",
            lambda session_id, cwd="": rollout,
        )

        assert await context_pct_for_session(sess) == 20

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


class TestBudgetForModel:
    """Per-model context-window denominator. Default is 1M; Haiku
    family (the only modern one that still ships with 200k) is the
    sole exception."""

    @pytest.mark.parametrize(
        "model",
        [
            "claude-haiku-4-5-20251001",
            "claude-haiku-4-5",
            "claude-haiku-3-5",
            "Claude-Haiku-4-5",  # case-insensitive
            "anthropic/claude-haiku-4-5",
        ],
    )
    def test_haiku_models_use_200k(self, model: str) -> None:
        from ccbot.usage import _budget_for_model

        assert _budget_for_model(model) == 200_000

    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-opus-4-5",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5",
            "claude-3-5-sonnet-20241022",
            "",  # empty → default 1M
            "unknown-model",
        ],
    )
    def test_non_haiku_models_use_1m(self, model: str) -> None:
        from ccbot.usage import _budget_for_model

        assert _budget_for_model(model) == 1_000_000
