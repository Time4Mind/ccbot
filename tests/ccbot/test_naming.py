"""Tests for naming._sanitize, _looks_default_name, maybe_auto_name."""

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.naming import (
    _build_naming_env,
    _looks_default_name,
    _sanitize,
    maybe_auto_name,
)
from ccbot.session import session_manager
from ccbot.session_models import Session


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


class TestLooksDefaultName:
    @pytest.mark.parametrize(
        "name,workdir",
        [
            ("workdir", "/home/x/workdir"),
            ("workdir-2", "/home/x/workdir"),
            ("workdir-99", "/home/x/workdir"),
            ("ccbot", "/home/x/pet_projects/ccbot"),
            ("", "/home/x/workdir"),
        ],
    )
    def test_default_patterns(self, name: str, workdir: str) -> None:
        assert _looks_default_name(name, workdir) is True

    @pytest.mark.parametrize(
        "name,workdir",
        [
            ("my-cool-feature", "/home/x/workdir"),
            ("scraper-fix", "/home/x/pet_projects/ccbot"),
            ("workdir-foo", "/home/x/workdir"),  # suffix not -<digits>
            ("workdir2", "/home/x/workdir"),  # no separator dash
            ("workdirx", "/home/x/workdir"),
        ],
    )
    def test_non_default(self, name: str, workdir: str) -> None:
        assert _looks_default_name(name, workdir) is False

    def test_missing_workdir(self) -> None:
        # Without a workdir to compare against, only the empty name is
        # considered default; an arbitrary name passes through.
        assert _looks_default_name("anything", "") is False
        assert _looks_default_name("", "") is True


class TestMaybeAutoName:
    @pytest.fixture
    def fresh_session(self):
        sess = Session(
            id="abcd1234",
            name="workdir",
            workdir="/tmp/workdir",
            state="active",
        )
        with (
            patch.object(session_manager, "get_session", return_value=sess),
            patch.object(session_manager, "rename_session") as rename_mock,
        ):
            yield sess, rename_mock

    @pytest.mark.asyncio
    async def test_setting_off_skips_haiku(
        self, fresh_session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sess, rename_mock = fresh_session
        monkeypatch.setattr(
            session_manager,
            "get_user_settings",
            lambda _uid: {"haiku_naming": False},
        )
        gen = AsyncMock(return_value="some-name")
        monkeypatch.setattr("ccbot.naming.generate_name", gen)
        await maybe_auto_name(sess.id, "long enough seed text", user_id=42)
        gen.assert_not_awaited()
        rename_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_setting_on_calls_haiku(
        self, fresh_session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sess, rename_mock = fresh_session
        monkeypatch.setattr(
            session_manager,
            "get_user_settings",
            lambda _uid: {"haiku_naming": True},
        )
        gen = AsyncMock(return_value="token-budget-alerts")
        monkeypatch.setattr("ccbot.naming.generate_name", gen)
        await maybe_auto_name(sess.id, "investigate token-budget alerts", user_id=42)
        gen.assert_awaited_once()
        rename_mock.assert_called_once_with(sess.id, "token-budget-alerts")

    @pytest.mark.asyncio
    async def test_skips_manually_renamed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sess = Session(
            id="abcd1234",
            name="my-feature",
            workdir="/tmp/workdir",
            state="active",
        )
        with (
            patch.object(session_manager, "get_session", return_value=sess),
            patch.object(session_manager, "rename_session") as rename_mock,
        ):
            monkeypatch.setattr(
                session_manager,
                "get_user_settings",
                lambda _uid: {"haiku_naming": True},
            )
            gen = AsyncMock(return_value="overwritten")
            monkeypatch.setattr("ccbot.naming.generate_name", gen)
            await maybe_auto_name(sess.id, "any seed", user_id=42)
            gen.assert_not_awaited()
            rename_mock.assert_not_called()
