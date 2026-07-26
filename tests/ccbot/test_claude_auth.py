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
    def test_matches_the_cli_wording(self) -> None:
        # Captured from `claude -p` against an expired store.
        assert claude_auth.looks_like_auth_failure(
            "Failed to authenticate: OAuth session expired and could not be refreshed"
        )

    @pytest.mark.parametrize(
        "text",
        ["Please run /login", "Invalid API key · Fix external API key"],
    )
    def test_matches_other_markers(self, text: str) -> None:
        assert claude_auth.looks_like_auth_failure(text)

    @pytest.mark.parametrize("text", ["", "all good", "authenticated fine"])
    def test_ignores_unrelated_output(self, text: str) -> None:
        assert not claude_auth.looks_like_auth_failure(text)


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
