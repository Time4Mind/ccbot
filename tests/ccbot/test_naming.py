"""Tests for naming._sanitize."""

import pytest

from ccbot.naming import _build_naming_env, _sanitize


class TestSanitize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("frontend-redesign", "frontend-redesign"),
            ("FRONTEND REDESIGN", "frontend-redesign"),
            ("auth_backend!!", "auth-backend"),
            ("  scrape-linkedin  ", "scrape-linkedin"),
            ("`linkedin-scraper`", "linkedin-scraper"),
            ('"my session"', "my-session"),
            ("ok\nextra fluff", "ok"),
        ],
    )
    def test_normal_inputs(self, raw: str, expected: str) -> None:
        assert _sanitize(raw) == expected

    def test_too_long_rejected(self) -> None:
        # Output regex caps at 32 chars total.
        assert _sanitize("a" * 50) == ""

    def test_empty_returns_empty(self) -> None:
        assert _sanitize("") == ""

    def test_starts_with_digit_rejected(self) -> None:
        # Regex requires leading [a-z].
        assert _sanitize("123-abc") == ""

    def test_double_dashes_collapsed(self) -> None:
        assert _sanitize("foo----bar") == "foo-bar"


class TestBuildNamingEnv:
    def test_forces_is_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Settings.json sets bypassPermissions, which under root requires
        # IS_SANDBOX=1 or claude refuses to start. Ccbot itself isn't
        # always launched with the var set (depends on whether the
        # supervisor was started from inside or outside a claude shell),
        # so naming has to force it.
        monkeypatch.delenv("IS_SANDBOX", raising=False)
        env = _build_naming_env()
        assert env.get("IS_SANDBOX") == "1"

    def test_scrubs_parent_claude_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When ccbot is restarted from within a claude session (the
        # typical dev workflow), the parent claude exports session-
        # specific env vars. Inheriting them into the haiku subprocess
        # makes it try to nest under the parent's session_id → wrong
        # cwd, stale tools, SessionStart hook writing the wrong row in
        # session_map.json.
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-sess-id")
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
        monkeypatch.setenv("CLAUDE_CODE_EXECPATH", "/some/path")
        monkeypatch.setenv("AI_AGENT", "claude-code_x_agent")
        env = _build_naming_env()
        for key in (
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDECODE",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_EXECPATH",
            "AI_AGENT",
        ):
            assert key not in env, f"{key} leaked into naming subprocess env"

    def test_preserves_unrelated_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Don't be over-zealous: PATH, HOME, terminal-bot config etc.
        # must still reach the subprocess.
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/root")
        env = _build_naming_env()
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOME"] == "/root"
