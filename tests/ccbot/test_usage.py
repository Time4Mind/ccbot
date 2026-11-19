"""Tests for usage.parse_session_usage and aggregate_session."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ccbot.session import Session, SessionManager
from ccbot.usage import context_pct_for_session, parse_session_usage


@pytest.mark.parametrize("language", ["en", "ru", "zh"])
def test_opencode_usage_labels_its_own_recorded_sessions(monkeypatch, language):
    from ccbot import i18n
    from ccbot.opencode_usage import OpenCodeUsageInfo
    from ccbot.usage import format_usage_breakdown_compact

    monkeypatch.setattr(i18n, "get_user_lang", lambda _uid: language)
    text = format_usage_breakdown_compact(
        42, OpenCodeUsageInfo(123, 100, 23, "gpt-6-luna")
    )
    assert "OpenCode" in text
    assert "ZCode" not in text
    assert "123" in text


@pytest.mark.parametrize(
    ("counts", "compact"),
    [
        ((10_000, 9_500, 500), ("10K", "9.5K", "500")),
        ((10_000_000, 9_999_000, 1_000), ("10M", "10M", "1K")),
        ((1_250_000_000, 999_999_999, 250_000_001), ("1.2B", "1B", "250M")),
    ],
)
def test_opencode_menu_compacts_all_token_counts(monkeypatch, counts, compact):
    from ccbot import i18n
    from ccbot.opencode_usage import OpenCodeUsageInfo
    from ccbot.usage import format_usage_breakdown_compact

    monkeypatch.setattr(i18n, "get_user_lang", lambda _uid: "en")
    total, incoming, outgoing = compact
    text = format_usage_breakdown_compact(42, OpenCodeUsageInfo(*counts))
    assert f"{total} tokens (input {incoming}, output {outgoing})" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("opencode_account", "codex_store", "shares_quota"),
    [
        ("shared", "file", True),
        ("other", "file", False),
        (None, "file", False),
        ("shared", "keyring", False),
    ],
)
async def test_opencode_menu_uses_live_codex_quota_only_for_same_account(
    tmp_path, monkeypatch, opencode_account, codex_store, shares_quota
):
    from ccbot import codex_usage, opencode_usage
    from ccbot.usage import format_usage_breakdown_compact

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        f'cli_auth_credentials_store = "{codex_store}"\n'
    )
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"refresh_token": "test", "account_id": "shared"},
            }
        )
    )
    data_home = tmp_path / "data"
    (data_home / "opencode").mkdir(parents=True)
    auth = data_home / "opencode/auth.json"
    auth.write_text(
        json.dumps(
            {
                "openai": {
                    "type": "oauth",
                    "refresh": "test",
                    "accountId": opencode_account,
                },
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)
    fetch = AsyncMock(
        return_value=codex_usage.CodexUsageInfo(
            weekly=codex_usage.CodexRateLimitWindow(13, 10_080, None),
        )
    )
    monkeypatch.setattr(codex_usage, "fetch_codex_usage", fetch)

    info = await opencode_usage.fetch_usage()
    rendered = format_usage_breakdown_compact(42, info)
    if shares_quota:
        assert "| OpenCode · Codex | - | 13% |" in rendered
        auth.unlink()
        assert "| OpenCode | - | - | - | - |" in format_usage_breakdown_compact(
            42, info
        )
        fetch.assert_awaited_once_with(allow_rollout_fallback=False)
    else:
        assert "| OpenCode | - | - | - | - |" in rendered
        fetch.assert_not_awaited()


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
