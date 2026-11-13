"""OpenCode must accept each queued prompt once while Stop stays available."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import tmux_input_transport
from ccbot.tmux_manager import TmuxManager

IDLE = "  ┃\n  ┃  Build · GPT-6 Luna OpenAI\n  /tmp/lab  24K (2%)  ctrl+p commands\n"
BUSY = IDLE.rsplit("  /tmp", 1)[0] + "  ■⬝⬝  esc interrupt  24K (2%) ctrl+p commands\n"


@pytest.fixture
def native_input(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / "opencode.db")
    db.executescript("""
        CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, title TEXT,
            parent_id TEXT, time_updated INTEGER);
        CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
    """)
    db.execute("INSERT INTO session VALUES ('ses_input', '/tmp/lab','test',NULL,1)")
    db.commit()
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCODE_SESSION_DB_PATH", str(tmp_path / "opencode.db"))
    (tmp_path / "session_map.json").write_text(
        json.dumps(
            {
                "test:@7": {
                    "backend": "opencode",
                    "session_id": "ses_input",
                    "cwd": "/tmp/lab",
                }
            }
        )
    )
    yield db
    db.close()


@pytest.mark.asyncio
async def test_busy_opencode_preserves_prompt_and_allows_stop(
    native_input, monkeypatch
):
    idle = asyncio.Event()
    capture_seen = asyncio.Event()

    async def process(*args, **kwargs):
        if "capture-pane" in args:
            capture_seen.set()
            output = IDLE if idle.is_set() else BUSY
        else:
            output = "opencode"
        return SimpleNamespace(
            returncode=0, communicate=AsyncMock(return_value=(output.encode(), b""))
        )

    async def paste(window, text, *, backend):
        assert idle.is_set(), "second request pasted while OpenCode is busy"
        now = int(time.time() * 1000)
        native_input.execute(
            "INSERT INTO message VALUES ('m1','ses_input',?,?,?)",
            (now, now, json.dumps({"role": "user"})),
        )
        native_input.execute(
            "INSERT INTO part VALUES ('p1','m1','ses_input',?,?,?)",
            (now, now, json.dumps({"type": "text", "text": text})),
        )
        native_input.commit()
        return True

    monkeypatch.setattr(tmux_input_transport.asyncio, "create_subprocess_exec", process)
    raw = AsyncMock(side_effect=paste)
    monkeypatch.setattr(tmux_input_transport, "_send_literal_chunked", raw)
    manager = TmuxManager()

    async def locked(window, text, enter, literal, *, backend):
        if literal:
            return await tmux_input_transport.send_literal_chunked(
                window, text, backend=backend
            )
        return True

    monkeypatch.setattr(manager, "_send_keys_locked", locked)
    task = asyncio.create_task(
        manager.send_keys("@7", "follow-up\n🙂", backend="opencode")
    )
    try:
        await asyncio.sleep(0.05)
        raw.assert_not_awaited()
        # A queued prompt must not hold the keyboard/navigation lock.
        assert await asyncio.wait_for(
            manager.send_keys(
                "@7", "Escape", enter=False, literal=False, backend="opencode"
            ),
            0.1,
        )
        idle.set()
        assert await asyncio.wait_for(task, 2)
        raw.assert_awaited_once_with("@7", "follow-up\n🙂", backend="opencode")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_old_matching_prompt_never_acknowledges_a_new_send(
    native_input, monkeypatch
):
    from ccbot import opencode_input

    native_input.execute(
        "INSERT INTO message VALUES ('old','ses_input',1,1,?)",
        (json.dumps({"role": "user"}),),
    )
    native_input.execute(
        "INSERT INTO part VALUES ('old-part','old','ses_input',1,1,?)",
        (json.dumps({"type": "text", "text": "same prompt"}),),
    )
    native_input.commit()
    monkeypatch.setattr(opencode_input, "_capture", AsyncMock(return_value=IDLE))
    monkeypatch.setattr(opencode_input, "ACK_TIMEOUT", 0.01)
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=True)
    )
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(tmux_input_transport, "_send_literal_chunked", send)
    assert not await tmux_input_transport.send_literal_chunked(
        "@7", "same prompt", backend="opencode"
    )
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_unready_or_dead_opencode_never_receives_queued_text(
    native_input, monkeypatch
):
    from ccbot import opencode_input

    monkeypatch.setattr(opencode_input, "_capture", AsyncMock(return_value=BUSY))
    monkeypatch.setattr(opencode_input, "READY_TIMEOUT", 0.01)
    monkeypatch.setattr(
        tmux_input_transport, "zcode_process_is_running", AsyncMock(return_value=False)
    )
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(tmux_input_transport, "_send_literal_chunked", send)
    assert not await tmux_input_transport.send_literal_chunked(
        "@7", "queued text", backend="opencode"
    )
    send.assert_not_awaited()
