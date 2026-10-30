"""Opt-in acceptance against the installed CLI, native hooks, tmux and provider."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

import pytest

from ccbot.node_worker import TmuxWorkerExecutor
from ccbot.zcode_hooks import install_hooks
from ccbot.zcode_runtime import composer_identity, interactive_panel, is_ready
from ccbot.zcode_session_io import reported_usage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CCBOT_RUN_ZCODE_LIVE") != "1",
        reason="opt-in real ZCode provider test",
    ),
]


async def run(*args: str, cwd: Path | None = None) -> dict:
    process = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout=25)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    assert process.returncode == 0, error.decode(errors="replace")[-500:]
    return json.loads(output) if output.strip() else {}


@pytest.fixture
def user_hook_config():
    """Exercise the actual supported user-hook entry and restore it exactly."""
    config = Path.home() / ".zcode/cli/config.json"
    original = config.read_bytes() if config.exists() else None
    mode = config.stat().st_mode & 0o777 if config.exists() else 0o600
    try:
        yield config
    finally:
        if original is None:
            config.unlink(missing_ok=True)
        else:
            config.write_bytes(original)
            config.chmod(mode)


@pytest.mark.asyncio
async def test_real_worker_hooks_tools_controls_and_native_resume(
    tmp_path, monkeypatch, user_hook_config
):
    bot_dir = tmp_path / "bot"
    database = tmp_path / "native.sqlite"
    monkeypatch.setenv("CCBOT_DIR", str(bot_dir))
    monkeypatch.setenv("ZCODE_SESSION_DB_PATH", str(database))
    monkeypatch.setenv("ZCODE_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("ZCODE_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ZCODE_MODEL_TELEMETRY_ENABLED", "0")
    (tmp_path / "input.txt").write_text("ZCODE_NATIVE_FILE_OK")
    launcher = tmp_path / "ccbot-test-hook"
    source = str(Path(__file__).resolve().parents[2] / "src")
    launcher.write_text(
        "#!/bin/sh\nprintf called\\n >> "
        + shlex.quote(str(tmp_path / "hook-called"))
        + "\nexec env PYTHONPATH="
        + shlex.quote(source)
        + " "
        + shlex.quote(sys.executable)
        + ' -m ccbot.main "$@" 2>>'
        + shlex.quote(str(tmp_path / "hook-errors"))
        + "\n"
    )
    launcher.chmod(0o700)
    project_config = tmp_path / "zcode.json"
    project_config.write_text(
        json.dumps(
            {
                "features": {
                    "mcp": False,
                    "subagent": False,
                    "memory": False,
                    "skill": False,
                },
                "plugins": {"enabled": False},
                "skills": {"enabled": False},
                "memory": {"use": False},
            }
        )
    )
    assert install_hooks(str(launcher), user_hook_config) == 0
    name = "ccbot-zcode-live-" + uuid.uuid4().hex[:10]
    worker = TmuxWorkerExecutor(workdir=tmp_path, tmux_session=name, ready_timeout=35)
    checks = {}
    try:
        result = await worker.create_session(
            path=str(tmp_path), backend="zcode", name="zcode-test"
        )
        routing_id = result["target_agent_session_id"]
        capture = await worker.capture_session(session_id=routing_id)
        assert is_ready(capture["pane"])
        checks["native_startup"] = True
        for command in ("/model", "/effort", "/mode"):
            assert (await worker.send_text(session_id=routing_id, text=command))["ok"]
            deadline = asyncio.get_running_loop().time() + 5
            panel = None
            while asyncio.get_running_loop().time() < deadline:
                panel = interactive_panel(
                    (await worker.capture_session(session_id=routing_id))["pane"]
                )
                if panel:
                    break
                await asyncio.sleep(0.15)
            assert panel and panel[0] == "Settings", command
            assert (await worker.send_key(session_id=routing_id, key="Escape"))["ok"]
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                if is_ready(
                    (await worker.capture_session(session_id=routing_id))["pane"]
                ):
                    break
                await asyncio.sleep(0.15)
            assert is_ready(
                (await worker.capture_session(session_id=routing_id))["pane"]
            ), command
            checks[command] = True
        assert (
            await worker.send_text(
                session_id=routing_id,
                text="Reply exactly ZCODE_SHORT_OK without tools.",
            )
        )["ok"]
        deadline = asyncio.get_running_loop().time() + 12
        initial_events = []
        while asyncio.get_running_loop().time() < deadline:
            initial_events.extend(await worker.poll_events())
            if (bot_dir / "session_map.json").exists():
                break
            await asyncio.sleep(0.25)
        assert (bot_dir / "session_map.json").exists(), {
            "hook_called": (tmp_path / "hook-called").exists(),
            "errors": (tmp_path / "hook-errors").read_text()
            if (tmp_path / "hook-errors").exists()
            else "",
            "pane": (await worker.capture_session(session_id=routing_id))["pane"][
                -2500:
            ],
        }
        deadline = asyncio.get_running_loop().time() + 45
        while asyncio.get_running_loop().time() < deadline:
            initial_events.extend(await worker.poll_events())
            if any(
                e.get("stop_reason") == "end_turn"
                and "ZCODE_SHORT_OK" in e.get("text", "")
                for e in initial_events
            ):
                break
            await asyncio.sleep(0.25)
        assert any(
            e.get("stop_reason") == "end_turn" and "ZCODE_SHORT_OK" in e.get("text", "")
            for e in initial_events
        )
        assert (
            await worker.send_text(
                session_id=routing_id,
                text=(
                    "Use Read to read input.txt and remember its contents for later. Then use Write "
                    "to create output.txt with exactly the same contents as input.txt. Do not use Bash. "
                    "Reply exactly ZCODE_LIVE_OK after the file has been written."
                ),
            )
        )["ok"]
        await asyncio.sleep(3)
        import sqlite3

        with sqlite3.connect(database) as connection:
            admitted = connection.execute("SELECT count(*) FROM session").fetchone()[0]
        assert admitted > 0, (await worker.capture_session(session_id=routing_id))[
            "pane"
        ]
        events = []
        deadline = asyncio.get_running_loop().time() + 100
        while asyncio.get_running_loop().time() < deadline:
            events.extend(await worker.poll_events())
            if any(
                e.get("stop_reason") == "end_turn"
                and "ZCODE_LIVE_OK" in e.get("text", "")
                for e in events
            ):
                break
            await asyncio.sleep(0.25)
        assert (tmp_path / "output.txt").read_text().strip() == "ZCODE_NATIVE_FILE_OK"
        assert any(
            e.get("role") == "user" and "input.txt" in e.get("text", "") for e in events
        )
        assert {e.get("tool_name") for e in events} >= {"Read", "Write"}
        assert any(e.get("content_type") == "tool_result" for e in events)
        assert any(
            e.get("stop_reason") == "end_turn" and "ZCODE_LIVE_OK" in e.get("text", "")
            for e in events
        )
        native = worker._sessions[routing_id]
        provider_id = native.provider_session_id
        path = native.transcript_path
        assert provider_id.startswith("sess_") and path is not None
        checks.update(
            native_hooks=True,
            normalized_user=True,
            read_write=True,
            tool_pairing=True,
            final_delivery=True,
        )
        usage = reported_usage(path)
        assert usage["total_tokens"] > 0
        checks["provider_usage"] = True
        assert (await worker.terminate_session(session_id=routing_id))["ok"]
        restored = await worker.create_session(
            path=str(tmp_path),
            backend="zcode",
            name="zcode-resume",
            resume_session_id=provider_id,
            source_backend="zcode",
            provider_transcript_path=str(path),
        )
        restored_id = restored["target_agent_session_id"]
        assert restored["provider_session_id"] == provider_id
        assert (
            await worker.send_text(
                session_id=restored_id,
                text="What marker did I ask you to remember? Reply only that marker, without tools.",
            )
        )["ok"]
        restored_events = []
        deadline = asyncio.get_running_loop().time() + 75
        while asyncio.get_running_loop().time() < deadline:
            restored_events.extend(await worker.poll_events())
            if any(
                e.get("stop_reason") == "end_turn"
                and "ZCODE_NATIVE_FILE_OK" in e.get("text", "")
                for e in restored_events
            ):
                break
            await asyncio.sleep(0.25)
        assert any(
            e.get("stop_reason") == "end_turn"
            and "ZCODE_NATIVE_FILE_OK" in e.get("text", "")
            for e in restored_events
        )
        assert worker._sessions[restored_id].provider_session_id == provider_id
        checks["native_resume_and_recall"] = True
        assert composer_identity(
            (await worker.capture_session(session_id=restored_id))["pane"]
        )
        if report := os.environ.get("CCBOT_ZCODE_LIVE_REPORT"):
            Path(report).write_text(json.dumps(checks, indent=2) + "\n")
    finally:
        process = await asyncio.create_subprocess_exec(
            "tmux",
            "kill-session",
            "-t",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
