"""Tests for the Telegram-driven Claude re-login (``ccbot.claude_auth``).

No real ``claude auth login`` is spawned: the pty driver is exercised through a
fake child that prints the same shapes the CLI does (verified against v2.1.220),
including the OSC-8 hyperlink duplication and the code prompt with no trailing
newline.
"""

import json
import time

import pytest

from ccbot import claude_auth


class TestAuthFailureDetection:
    def test_matches_the_real_error_entry(self) -> None:
        # Captured from a live session against a dead store: Claude Code writes
        # a synthetic assistant turn with isApiErrorMessage + this error code.
        assert claude_auth.is_auth_failure_event(
            "authentication_failed", "Login expired · Please run /login"
        )

    def test_generic_error_code_falls_back_to_the_wording(self) -> None:
        assert claude_auth.is_auth_failure_event(
            "unknown", "Failed to authenticate: OAuth session expired"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Login expired · Please run /login",
            "The error text is `Login expired · Please run /login` and it means "
            "the OAuth login died; send /login to the bot to fix it. Note that "
            "Please run /login is the marker we used to match on.",
        ],
    )
    def test_unflagged_text_never_counts(self, text: str) -> None:
        # THE regression: a session that merely writes about the failure (this
        # feature's own development session did, repeatedly) must not make the
        # bot announce that a healthy host lost its login.
        assert not claude_auth.is_auth_failure_event("", text)

    def test_long_prose_with_a_generic_code_is_ignored(self) -> None:
        prose = "Login expired · Please run /login " + "x" * 200
        assert not claude_auth.is_auth_failure_event("unknown", prose)

    @pytest.mark.parametrize("text", ["", "all good", "authenticated fine"])
    def test_unflagged_unrelated_output_is_ignored(self, text: str) -> None:
        assert not claude_auth.is_auth_failure_event("", text)

    def test_other_api_errors_are_not_auth_failures(self) -> None:
        # A flagged error of a different kind (quota, overload, network) must
        # not offer a re-login.
        assert not claude_auth.is_auth_failure_event(
            "rate_limit_error", "Usage limit reached · resets at 17:00"
        )


class TestApiErrorPlumbing:
    """The flag has to survive the parser, or the detector never sees it."""

    def _entry(self, text: str, **extra) -> dict:
        return {
            "type": "assistant",
            "timestamp": "2026-07-26T13:05:47.279Z",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": text}],
            },
            **extra,
        }

    def test_auth_error_entry_carries_the_code(self) -> None:
        from ccbot.transcript_parser import TranscriptParser

        entries, _ = TranscriptParser.parse_entries(
            [
                self._entry(
                    "Login expired · Please run /login",
                    isApiErrorMessage=True,
                    error="authentication_failed",
                )
            ]
        )
        assert entries
        assert entries[0].api_error == "authentication_failed"
        assert claude_auth.is_auth_failure_event(entries[0].api_error, entries[0].text)

    def test_plain_assistant_text_has_no_code(self) -> None:
        # Exactly this session's own case: writing *about* the error.
        from ccbot.transcript_parser import TranscriptParser

        entries, _ = TranscriptParser.parse_entries(
            [self._entry("the CLI prints `Login expired · Please run /login` on death")]
        )
        assert entries
        assert entries[0].api_error == ""
        assert not claude_auth.is_auth_failure_event(
            entries[0].api_error, entries[0].text
        )

    def test_flagged_entry_without_error_code_gets_a_placeholder(self) -> None:
        from ccbot.transcript_parser import TranscriptParser

        entries, _ = TranscriptParser.parse_entries(
            [self._entry("Login expired · Please run /login", isApiErrorMessage=True)]
        )
        assert entries[0].api_error == "unknown"
        assert claude_auth.is_auth_failure_event(entries[0].api_error, entries[0].text)


class TestCredentialsState:
    def _write(self, tmp_path, access_ms: int, refresh_ms: int):
        (tmp_path / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "x",
                        "refreshToken": "y",
                        "expiresAt": access_ms,
                        "refreshTokenExpiresAt": refresh_ms,
                        "subscriptionType": "max",
                    }
                }
            )
        )
        return tmp_path

    def test_reads_deadlines_in_seconds(self, tmp_path) -> None:
        now_ms = int(time.time() * 1000)
        path = self._write(tmp_path, now_ms + 3_600_000, now_ms + 30 * 86_400_000)
        state = claude_auth.credentials_state(path)
        assert state.present
        assert state.subscription == "max"
        assert state.refresh_alive
        assert state.access_expires_at == pytest.approx((now_ms + 3_600_000) / 1000)

    def test_expired_wall_is_not_alive(self, tmp_path) -> None:
        now_ms = int(time.time() * 1000)
        path = self._write(tmp_path, now_ms - 86_400_000, now_ms - 3_600_000)
        assert not claude_auth.credentials_state(path).refresh_alive

    def test_missing_file(self, tmp_path) -> None:
        assert not claude_auth.credentials_state(tmp_path).present

    def test_api_key_store_is_not_oauth(self, tmp_path) -> None:
        # Third-party / API-key configs have no claudeAiOauth block; they must
        # not read as "present" or the flow would report a bogus deadline.
        (tmp_path / ".credentials.json").write_text(json.dumps({"other": {}}))
        assert not claude_auth.credentials_state(tmp_path).present


# A stand-in for `claude auth login`: prints the URL exactly as the CLI does
# (OSC-8 wrapper, URL repeated inside it), then blocks on a prompt that carries
# no newline, then echoes success once a line arrives on stdin.
_FAKE_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=abc"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&code_challenge=xyz&state=st"
)
_FAKE_LOGIN = f"""
import json, os, sys, time
sys.stdout.write("Opening browser to sign in\\u2026\\r\\n")
sys.stdout.write("If the browser didn't open, visit: \\x1b]8;;{_FAKE_URL}\\x1b\\\\{_FAKE_URL}\\x1b]8;;\\x1b\\\\\\r\\n")
sys.stdout.write("Paste code here if prompted > ")
sys.stdout.flush()
line = sys.stdin.readline().strip()
if line == "good-code":
    # The real CLI rewrites the credential store on a successful exchange.
    now_ms = int(time.time() * 1000)
    wall = now_ms + 30 * 86_400_000
    store = os.environ["CLAUDE_CONFIG_DIR"]
    with open(os.path.join(store, ".credentials.json"), "w") as fh:
        json.dump({{"claudeAiOauth": {{"accessToken": "new", "refreshToken": "new",
                   "expiresAt": now_ms + 8 * 3_600_000,
                   "refreshTokenExpiresAt": wall}}}}, fh)
    sys.stdout.write("\\r\\nLogin successful\\r\\n")
else:
    sys.stdout.write("\\r\\nInvalid code\\r\\n")
sys.stdout.flush()
"""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """Point the flow at a fake CLI and an isolated credential store."""
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_LOGIN)
    shim = tmp_path / "fake-claude"
    shim.write_text(f'#!/bin/sh\nexec python3 "{script}"\n')
    shim.chmod(0o755)
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(store))
    return str(shim), store


def _write_wall(store, refresh_ms: int) -> None:
    (store / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a",
                    "refreshToken": "r",
                    "expiresAt": refresh_ms,
                    "refreshTokenExpiresAt": refresh_ms,
                }
            }
        )
    )


class TestLoginFlow:
    @pytest.mark.asyncio
    async def test_start_extracts_a_single_clean_url(self, fake_cli) -> None:
        command, _ = fake_cli
        flow = claude_auth.LoginFlow(1, command=command)
        try:
            url = await flow.start()
        finally:
            flow.cancel()
        # The OSC-8 wrapper repeats the URL; the parser must yield it once,
        # with no escape residue.
        assert url == _FAKE_URL

    @pytest.mark.asyncio
    async def test_submit_code_succeeds_when_the_wall_moves(self, fake_cli) -> None:
        command, store = fake_cli
        now_ms = int(time.time() * 1000)
        _write_wall(store, now_ms - 1000)  # currently dead
        flow = claude_auth.LoginFlow(2, command=command)
        assert await flow.start()
        ok, detail = await flow.submit_code("good-code")
        assert ok, detail
        # Success is judged by the store, which the child rewrote mid-exchange.
        assert claude_auth.credentials_state(store).refresh_alive

    @pytest.mark.asyncio
    async def test_submit_code_fails_when_the_store_does_not_move(
        self, fake_cli
    ) -> None:
        command, store = fake_cli
        now_ms = int(time.time() * 1000)
        _write_wall(store, now_ms + 86_400_000)
        flow = claude_auth.LoginFlow(3, command=command)
        assert await flow.start()
        ok, detail = await flow.submit_code("bad-code")
        # Credential store is the source of truth: unchanged wall == failure,
        # whatever the CLI printed.
        assert not ok
        assert detail

    @pytest.mark.asyncio
    async def test_no_url_when_the_cli_says_nothing(self, tmp_path) -> None:
        silent = tmp_path / "silent"
        silent.write_text("#!/bin/sh\nexit 0\n")
        silent.chmod(0o755)
        flow = claude_auth.LoginFlow(4, command=str(silent))
        assert await flow.start() == ""

    @pytest.mark.asyncio
    async def test_login_does_not_inherit_tmux(self, fake_cli, monkeypatch) -> None:
        # The ccbot SessionStart hook keys off $TMUX; a login that inherited it
        # would rewrite session_map.json and hijack a live window's mapping.
        monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,1,0")
        command, _ = fake_cli
        flow = claude_auth.LoginFlow(5, command=command)
        try:
            assert await flow.start()
            assert flow._proc is not None
        finally:
            flow.cancel()


class TestFlowRegistry:
    @pytest.mark.asyncio
    async def test_start_flow_registers_and_drop_clears(self, fake_cli) -> None:
        command, _ = fake_cli
        flow = await claude_auth.start_flow(7, command=command)
        assert flow is not None
        assert claude_auth.get_flow(7) is flow
        claude_auth.drop_flow(7)
        assert claude_auth.get_flow(7) is None

    @pytest.mark.asyncio
    async def test_expired_flow_is_dropped_on_read(self, fake_cli) -> None:
        command, _ = fake_cli
        flow = await claude_auth.start_flow(8, command=command)
        assert flow is not None
        # A forgotten flow must not keep swallowing later messages as codes.
        flow.created_at = time.time() - claude_auth.FLOW_TTL - 1
        assert claude_auth.get_flow(8) is None

    @pytest.mark.asyncio
    async def test_start_flow_replaces_a_pending_one(self, fake_cli) -> None:
        command, _ = fake_cli
        first = await claude_auth.start_flow(9, command=command)
        second = await claude_auth.start_flow(9, command=command)
        assert first is not second
        assert claude_auth.get_flow(9) is second
        claude_auth.drop_flow(9)

    @pytest.mark.asyncio
    async def test_failed_start_is_not_registered(self, tmp_path) -> None:
        silent = tmp_path / "silent"
        silent.write_text("#!/bin/sh\nexit 0\n")
        silent.chmod(0o755)
        assert await claude_auth.start_flow(10, command=str(silent)) is None
        assert claude_auth.get_flow(10) is None


class TestLoginMessages:
    """The URL message is the one thing the whole flow depends on."""

    @pytest.mark.asyncio
    async def test_url_message_kwargs_survive_ptb_validation(
        self, monkeypatch, fake_cli
    ) -> None:
        # safe_send already defaults link_preview_options; passing
        # disable_web_page_preview alongside it makes PTB raise ValueError
        # ("mutually exclusive") and the link never reaches the chat.
        from telegram._utils.argumentparsing import parse_lpo_and_dwpp

        from ccbot.bot.commands import auth as auth_cmd
        from ccbot.handlers.message_sender import NO_LINK_PREVIEW

        command, _ = fake_cli
        monkeypatch.setattr(auth_cmd.config, "claude_command", command)
        sent: list[dict] = []

        async def fake_safe_send(bot, chat_id, text, **kwargs):
            sent.append({"text": text, **kwargs})
            return None

        monkeypatch.setattr(auth_cmd, "safe_send", fake_safe_send)
        assert await auth_cmd.start_login(object(), 42) is True
        claude_auth.drop_flow(42)

        url_msg = next(m for m in sent if _FAKE_URL in m["text"])
        # Emulate what safe_send does before calling PTB.
        kwargs = {k: v for k, v in url_msg.items() if k != "text"}
        kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
        parse_lpo_and_dwpp(
            kwargs.get("disable_web_page_preview"), kwargs["link_preview_options"]
        )

    @pytest.mark.asyncio
    async def test_failure_message_when_no_url(self, monkeypatch, tmp_path) -> None:
        from ccbot.bot.commands import auth as auth_cmd

        silent = tmp_path / "silent"
        silent.write_text("#!/bin/sh\nexit 0\n")
        silent.chmod(0o755)
        monkeypatch.setattr(auth_cmd.config, "claude_command", str(silent))
        sent: list[str] = []

        async def fake_safe_send(bot, chat_id, text, **kwargs):
            sent.append(text)
            return None

        monkeypatch.setattr(auth_cmd, "safe_send", fake_safe_send)
        assert await auth_cmd.start_login(object(), 43) is False
        assert any("/login" in s for s in sent)


class TestPostLoginSurface:
    """A bare "logged in" line leaves the user with nothing to tap."""

    @pytest.fixture
    def wired(self, monkeypatch, fake_cli):
        from ccbot.bot.commands import auth as auth_cmd

        command, store = fake_cli
        monkeypatch.setattr(auth_cmd.config, "claude_command", command)
        _write_wall(store, int(time.time() * 1000) - 1000)
        sent: list[dict] = []
        reposted: list[object] = []

        async def fake_safe_send(bot, chat_id, text, **kwargs):
            sent.append({"text": text, **kwargs})
            return None

        async def fake_repost(bot, user_id, sess):
            reposted.append(sess)

        monkeypatch.setattr(auth_cmd, "safe_send", fake_safe_send)
        monkeypatch.setattr(auth_cmd, "repost_card", fake_repost)
        return auth_cmd, command, sent, reposted

    class _Msg:
        def __init__(self, text: str) -> None:
            self.text = text
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

    class _Update:
        def __init__(self, user_id: int, message) -> None:
            self.effective_user = type("U", (), {"id": user_id})()
            self.message = message

    class _Ctx:
        bot = object()

    @pytest.mark.asyncio
    async def test_active_session_card_is_reposted(self, wired, monkeypatch) -> None:
        auth_cmd, command, sent, reposted = wired
        sess = object()
        monkeypatch.setattr(
            auth_cmd.session_manager,
            "get_active_session",
            lambda uid: type("S", (), {"window_id": "@7"})(),
        )
        flow = await claude_auth.start_flow(70, command=command)
        assert flow is not None
        msg = self._Msg("good-code")
        assert await auth_cmd.maybe_consume_code(self._Update(70, msg), self._Ctx())
        assert any("✅" in m["text"] or "Готово" in m["text"] for m in sent)
        # The working surface has to follow the confirmation.
        assert reposted, "active session card was not reposted after login"
        assert msg.deleted, "the pasted code must not stay in the chat"
        assert sess is not None

    @pytest.mark.asyncio
    async def test_menu_fallback_without_active_session(
        self, wired, monkeypatch
    ) -> None:
        auth_cmd, command, sent, reposted = wired
        monkeypatch.setattr(
            auth_cmd.session_manager, "get_active_session", lambda uid: None
        )
        monkeypatch.setattr(auth_cmd, "render_more_text", lambda uid: "MENU-TEXT")
        monkeypatch.setattr(auth_cmd, "build_footer_keyboard", lambda uid, screen: None)
        flow = await claude_auth.start_flow(71, command=command)
        assert flow is not None
        assert await auth_cmd.maybe_consume_code(
            self._Update(71, self._Msg("good-code")), self._Ctx()
        )
        assert not reposted
        assert any(m["text"] == "MENU-TEXT" for m in sent), (
            "no menu surface after login without an active session"
        )
