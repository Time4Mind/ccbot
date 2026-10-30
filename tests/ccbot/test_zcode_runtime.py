"""ZCode UI, hook installation, input safety and worker capability regressions."""

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import hook, tmux_input_transport
from ccbot.node_backend_readiness import parse_backend_health, probe_ready_backends
from ccbot.node_worker import TmuxWorkerExecutor
from ccbot.terminal_parser import extract_interactive_content, parse_status_line
from ccbot.zcode_hooks import install_hooks
from ccbot.zcode_runtime import auth_state, composer_identity, is_ready, navigation_key
from ccbot.zcode_usage import ZCodeUsageInfo

SID = "sess_12345678-1234-1234-1234-123456789abc"
COMPOSER = """╭─ Yolo ───────────────────────────────────────────────╮
│ Type a prompt                                        │
│                                                      │
│                                                      │
│ glm-4.5-flash zai-free | disabled                      │
╰──────────────────────────────────────────────────────╯"""
MODELS = """╭─ Models ─────────────────────────────────────────────╮
│ > glm-4.5-flash       Z.ai Free                        │
│   glm-4.7-flash       Z.ai Free                        │
╰──────────────────────────────────────────────────────╯"""


def test_composer_and_panel_are_anchored_to_native_chrome():
    assert is_ready(COMPOSER)
    assert composer_identity(COMPOSER) == ("glm-4.5-flash", "disabled")
    assert composer_identity(COMPOSER + "\n16K (8%)") == ("glm-4.5-flash", "disabled")
    assert (
        parse_status_line(COMPOSER + "\n⠋ esc to interrupt     16K (8%)") == "⠋ Working"
    )
    assert not is_ready(COMPOSER + "\nroot@host:~#")
    assert composer_identity("glm-4.5-flash zai-free | disabled") is None
    panel = extract_interactive_content(MODELS + "\n" + COMPOSER)
    assert panel and panel.name == "Settings" and "glm-4.7-flash" in panel.content
    assert extract_interactive_content(MODELS) is None
    assert (
        navigation_key(
            "Escape", MODELS + "\n" + COMPOSER.replace("Type a prompt", "/model")
        )
        == "C-u"
    )
    assert navigation_key("Escape", COMPOSER) == "Escape"
    assert parse_status_line("⠋ esc to interrupt\n" + COMPOSER) == "⠋ Working"


def test_hook_install_preserves_other_settings_and_is_idempotent(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "ui": {"locale": "en-US"},
                "hooks": {
                    "events": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "existing"}]}
                        ]
                    }
                },
            }
        )
    )
    assert install_hooks("/opt/ccbot", path) == 0
    first = path.read_bytes()
    assert install_hooks("/opt/ccbot", path) == 0
    assert path.read_bytes() == first
    result = json.loads(first)
    assert result["ui"]["locale"] == "en-US"
    assert result["hooks"]["events"]["Stop"][0]["hooks"][0]["command"] == "existing"
    assert result["hooks"]["events"]["SessionStart"][0]["hooks"][0]["args"] == [
        "hook",
        "--backend",
        "zcode",
    ]


def test_zcode_hook_retains_native_namespace_and_owns_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    monkeypatch.setenv("TMUX_PANE", "%99")
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout="ccbot::@9:test\n"),
    )
    payload = {
        "session_id": SID,
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "transcript_path": "/tmp/disposable-hook-transcript",
    }
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(payload)))
    hook._process_hook_event(backend="zcode")
    binding = json.loads((tmp_path / "session_map.json").read_text())["ccbot:@9"]
    assert binding["backend"] == "zcode" and binding["session_id"] == SID
    assert binding["transcript_path"] != payload["transcript_path"]
    assert Path(binding["transcript_path"]).is_file()


def test_auth_status_never_returns_or_prints_key(tmp_path, monkeypatch):
    path = tmp_path / "providers.json"
    monkeypatch.setenv("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", str(path))
    config = {
        "schemaVersion": 1,
        "config": {
            "defaultModelSelection": {"providerId": "test", "modelId": "free-model"},
            "providerConfigRules": {
                "providerRules": [
                    {
                        "providerId": "test",
                        "config": {
                            "api": {"baseUrl": "https://api.example.invalid"},
                            "access": {"type": "api-key", "apiKey": "test-secret"},
                        },
                    }
                ]
            },
        },
    }
    path.write_text(json.dumps(config))
    assert auth_state().configured
    assert "test-secret" not in repr(auth_state())
    config["config"]["providerConfigRules"]["providerRules"][0]["config"]["access"][
        "apiKey"
    ] = ""
    path.write_text(json.dumps(config))
    assert not auth_state().configured


@pytest.mark.asyncio
async def test_picker_input_waits_for_user_choice_and_refuses_shell(monkeypatch):
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )
    paste = AsyncMock(return_value=(True, False))
    submit = AsyncMock(return_value=True)
    monkeypatch.setattr(tmux_input_transport, "_paste_chunk", paste)
    monkeypatch.setattr(tmux_input_transport, "_send_carriage_return", submit)
    assert await tmux_input_transport.send_literal_chunked(
        "@9", "/model", backend="zcode"
    )
    submit.assert_not_called()
    assert await tmux_input_transport.send_literal_chunked(
        "@9", "/model zai-free/glm-4.7-flash", backend="zcode"
    )
    submit.assert_awaited_once()
    paste.reset_mock()
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=False)
    )
    assert not await tmux_input_transport.send_literal_chunked(
        "@9", "rm unrelated", backend="zcode"
    )
    paste.assert_not_called()


@pytest.mark.asyncio
async def test_worker_command_resume_and_readiness(tmp_path, monkeypatch):
    worker = TmuxWorkerExecutor(
        workdir=tmp_path,
        zcode_command="/opt/zcode",
        zcode_flags="--mode yolo",
        ready_timeout=0.2,
    )
    command = worker._agent_command(
        "zcode", resume_session_id=SID, initial_prompt="Do not append me"
    )
    assert "/opt/zcode --mode yolo --resume " + SID in command
    assert "CCBOT_AGENT_BACKEND=zcode" in command
    assert "Do not append me" not in command
    monkeypatch.setattr(worker, "_run_tmux", AsyncMock(return_value=(0, COMPOSER, "")))
    await worker._wait_ready("@9", "zcode")
    ready, configured, _ = parse_backend_health(
        {"backends": ["zcode"], "configured_backends": ["claude", "codex", "zcode"]}, []
    )
    assert ready == ["zcode"] and configured == ["claude", "codex", "zcode"]


@pytest.mark.asyncio
async def test_unconfigured_zcode_never_launches_login_status(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", str(tmp_path / "missing.json")
    )
    monkeypatch.setattr(
        "ccbot.node_backend_readiness.shutil.which", lambda _name: "/opt/zcode"
    )
    launch = AsyncMock()
    monkeypatch.setattr(
        "ccbot.node_backend_readiness.asyncio.create_subprocess_exec", launch
    )
    assert await probe_ready_backends(("zcode",), commands={"zcode": "zcode"}) == ()
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_naming_check_cannot_rebind_the_parent_tmux_window(monkeypatch):
    from ccbot.zcode_generation import generate

    monkeypatch.setenv("TMUX", "/private/socket,1,0")
    monkeypatch.setenv("TMUX_PANE", "%parent")
    monkeypatch.setenv("CCBOT_DIR", "/private/bot")
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b'{"response":"Test name"}', b"")),
    )
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr("ccbot.zcode_generation.asyncio.create_subprocess_exec", launch)
    assert await generate("zcode", "Give this task a short name") == "Test name"
    environment = launch.call_args.kwargs["env"]
    assert not {"TMUX", "TMUX_PANE", "CCBOT_DIR"} & environment.keys()
    assert not Path(environment["ZCODE_SESSION_DB_PATH"]).parent.exists()


@pytest.mark.asyncio
async def test_worker_readiness_reprobes_when_enabled_backends_change(
    tmp_path, monkeypatch
):
    worker = TmuxWorkerExecutor(workdir=tmp_path)
    probe = AsyncMock(side_effect=[("codex",), ("codex", "zcode")])
    monkeypatch.setattr("ccbot.node_worker.probe_ready_backends", probe)
    assert await worker.ready_backends(("codex",)) == ("codex",)
    assert await worker.ready_backends(("codex", "zcode")) == ("codex", "zcode")
    assert probe.await_count == 2


def test_unknown_account_limits_are_not_reported_as_zero():
    from ccbot.usage import format_usage_breakdown_compact

    result = format_usage_breakdown_compact(
        12345, ZCodeUsageInfo(200, 150, 50, "glm-4.5-flash")
    )
    assert "ZCode" in result and "200 tokens" in result
    assert "not reported" in result
    assert "0%" not in result
