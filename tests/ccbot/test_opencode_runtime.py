"""OpenCode's UI and lifecycle are independent from the other backends."""

import io
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import hook
from ccbot.node_worker import TmuxWorkerExecutor
from ccbot.opencode_hooks import install_hooks
from ccbot.opencode_runtime import auth_state, is_ready
from ccbot.session import SessionManager
from ccbot.terminal_parser import extract_interactive_content, parse_status_line

COMPOSER = """
        ┃
        ┃  Ask anything… "What is the tech stack of this project?"
        ┃
        ┃  Build · Big Pickle OpenCode
        ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
                       tab agents  ctrl+p commands

        ● Tip Run /connect to add an AI provider and start coding

  /private/tmp                                      1.19.5
"""
MODELS = """
              Select model                            esc
              Search
              Nemotron 3.5 Super VL Free              Free
            ● Big Pickle                              Free
              Popular providers
              OpenAI (ChatGPT Plus/Pro or API key)
              View all providers ctrl+a
"""


def test_live_composer_and_model_menu_do_not_match_shell_or_codex():
    assert is_ready(COMPOSER)
    assert not is_ready(COMPOSER + "\nuser@host %")
    assert not is_ready(MODELS + COMPOSER)
    panel = extract_interactive_content(MODELS + COMPOSER)
    assert panel and panel.name == "Settings" and "OpenAI" in panel.content
    assert (
        parse_status_line("• Compacting context (18s • esc to interrupt)\n› Ask Codex")
        != "Working"
    )
    assert parse_status_line("BUILD  ■ esc interrupt       ctrl+p cmd") == "Working"


def test_opencode_hook_retains_native_id_and_project(tmp_path, monkeypatch):
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    monkeypatch.setenv("TMUX_PANE", "%99")
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout="ccbot::@9:test\n"),
    )
    payload = {
        "session_id": "ses_hookAcceptance123",
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
    }
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(payload)))
    hook._process_hook_event(backend="opencode")
    binding = json.loads((tmp_path / "session_map.json").read_text())["ccbot:@9"]
    assert binding["backend"] == "opencode"
    assert binding["session_id"] == "ses_hookAcceptance123"
    assert Path(binding["transcript_path"]).is_file()
    assert "opencode-transcripts" in binding["transcript_path"]


def test_plugin_install_is_idempotent_and_preserves_global_config(tmp_path):
    config = tmp_path / "opencode.json"
    config.write_text('{"model":"openai/gpt-6-luna"}')
    plugin = tmp_path / "plugins/ccbot.js"
    assert install_hooks("/path with spaces/ccbot", plugin) == 0
    first = plugin.read_bytes()
    assert install_hooks("/path with spaces/ccbot", plugin) == 0
    assert plugin.read_bytes() == first
    assert config.read_text() == '{"model":"openai/gpt-6-luna"}'
    assert b'"/path with spaces/ccbot"' in first


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_native_plugin_publishes_user_session_to_exact_hook_command(tmp_path):
    plugin = tmp_path / "ccbot.mjs"
    receiver = tmp_path / "fake ccbot"
    result = tmp_path / "received.json"
    receiver.write_text(
        f'#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\nPath({str(result)!r}).write_text(json.dumps({{"args":sys.argv[1:],"payload":p}}))\n'
    )
    receiver.chmod(0o700)
    assert install_hooks(str(receiver), plugin) == 0
    code = f"""import {{ CCBotPlugin }} from {json.dumps(plugin.as_uri())};
const p = await CCBotPlugin({{ directory: {json.dumps(str(tmp_path))} }});
await p.event({{ event: {{ type: "message.updated", properties: {{ info: {{ role: "user", sessionID: "ses_plugin123" }} }} }} }});
"""
    completed = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", code],
        env=os.environ | {"TMUX_PANE": "%99", "CCBOT_AGENT_BACKEND": "opencode"},
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    received = json.loads(result.read_text())
    assert received == {
        "args": ["hook", "--backend", "opencode"],
        "payload": {
            "session_id": "ses_plugin123",
            "cwd": str(tmp_path),
            "hook_event_name": "UserPromptSubmit",
        },
    }


def test_subscription_presence_never_accepts_api_key_or_exposes_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = tmp_path / "opencode/auth.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"openai": {"type": "api", "key": "test-secret"}}))
    assert not auth_state().configured
    path.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "test-secret"}}))
    assert auth_state().configured
    assert "test-secret" not in repr(auth_state())


def test_worker_command_uses_native_resume_and_luna_without_shell_prompt(tmp_path):
    executor = TmuxWorkerExecutor(
        workdir=tmp_path, opencode_command="opencode", opencode_flags=""
    )
    args = shlex.split(
        executor._agent_command("opencode", resume_session_id="ses_restore123")
    )
    assert args[args.index("--session") + 1] == "ses_restore123"
    assert args[args.index("--model") + 1] == "openai/gpt-6-luna"
    assert "--resume" not in args


@pytest.mark.asyncio
async def test_unconnected_subscription_blocks_local_and_worker_start(
    tmp_path, monkeypatch
):
    from ccbot.tmux_window import create_window
    import logging

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty"))
    result = await create_window(
        object(),
        str(tmp_path),
        backend="opencode",
        config_obj=SimpleNamespace(agent_backend="opencode"),
        logger_obj=logging.getLogger(__name__),
    )
    assert not result[0] and "subscription is not connected" in result[1]
    executor = TmuxWorkerExecutor(workdir=tmp_path)
    with pytest.raises(RuntimeError, match="subscription is not connected"):
        await executor.create_session(
            path=str(tmp_path), backend="opencode", name="test"
        )


def test_enabling_opencode_keeps_existing_session_backend(tmp_path, monkeypatch):
    from ccbot.session_models import Session
    from ccbot.config import config

    monkeypatch.setattr(config, "agent_backend", "codex")
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(config, "summary_cache_file", tmp_path / "summary.json")
    manager = SessionManager()
    manager.agent_backend = "codex"
    existing = Session(id="existing", name="Codex", backend="codex")
    manager.sessions[existing.id] = existing
    manager.set_backend_enabled(42, "opencode", True)
    assert manager.get_enabled_backends(42) == ("codex", "opencode")
    assert manager.sessions[existing.id].backend == "codex"


@pytest.mark.asyncio
async def test_opencode_auth_error_does_not_start_claude_login(monkeypatch):
    from ccbot.bot import session_events
    from ccbot.config import config
    from ccbot.handlers import message_sender
    from ccbot.session_monitor import NewMessage

    target = SimpleNamespace(backend="opencode")
    monkeypatch.setattr(config, "allowed_users", {42})
    monkeypatch.setattr(
        session_events.session_manager,
        "all_user_sessions_with_claude_id",
        lambda _sid: [(42, target)],
    )
    sent = AsyncMock()
    claude_login = AsyncMock()
    monkeypatch.setattr(message_sender, "safe_send", sent)
    monkeypatch.setattr(session_events, "notify_auth_expired", claude_login)
    monkeypatch.setattr(session_events, "_opencode_auth_notified", set())
    msg = NewMessage(
        session_id="ses_auth123",
        text="",
        is_complete=True,
        api_error="authentication_failed",
    )
    await session_events.handle_new_message(msg, AsyncMock())
    await session_events.handle_new_message(msg, AsyncMock())
    assert sent.await_count == 1
    assert "OpenCode" in sent.await_args.args[2]
    claude_login.assert_not_awaited()
