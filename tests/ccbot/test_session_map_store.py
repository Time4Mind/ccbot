from __future__ import annotations

import asyncio
import json

import pytest

from ccbot.config import config
from ccbot.session import SessionManager
from ccbot.session_map_store import upsert_session_map_entry
from ccbot.session_models import Session


def test_upsert_preserves_unrelated_rows_and_existing_fields(tmp_path):
    map_file = tmp_path / "session_map.json"
    map_file.write_text(
        json.dumps(
            {
                "ccbot:@1": {"session_id": "one", "transcript_path": "/one"},
                "ccbot:@9": {"hook_only": "preserved"},
            }
        )
    )

    row = upsert_session_map_entry(
        map_file,
        "ccbot:@9",
        {
            "session_id": "nine",
            "backend": "codex",
            "transcript_path": "/nine",
        },
    )

    assert row["hook_only"] == "preserved"
    data = json.loads(map_file.read_text())
    assert data["ccbot:@1"]["session_id"] == "one"
    assert data["ccbot:@9"]["session_id"] == "nine"
    assert data["ccbot:@9"]["transcript_path"] == "/nine"


@pytest.mark.asyncio
async def test_restore_publish_waits_for_inflight_map_reconcile(tmp_path, monkeypatch):
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
    monkeypatch.setattr(config, "tmux_session_name", "ccbot")
    manager = SessionManager()
    monkeypatch.setattr(manager, "save_state", lambda: None)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n")
    sess = Session(
        id="deadbeef",
        name="heavy",
        state="archived",
        backend="codex",
        workdir=str(tmp_path / "workdir"),
        claude_session_id="rollout-id",
    )
    manager.sessions[sess.id] = sess

    reconcile_started = asyncio.Event()
    release_reconcile = asyncio.Event()

    async def blocked_reconcile() -> None:
        reconcile_started.set()
        await release_reconcile.wait()

    monkeypatch.setattr(manager, "_load_session_map_unlocked", blocked_reconcile)
    reconcile_task = asyncio.create_task(manager.load_session_map())
    await reconcile_started.wait()
    publish_task = asyncio.create_task(
        manager.publish_codex_restore_binding(
            sess=sess,
            user_id=42,
            window_id="@9",
            window_name="heavy",
            transcript_path=transcript,
        )
    )
    await asyncio.sleep(0)
    assert not publish_task.done()

    release_reconcile.set()
    await reconcile_task
    await publish_task

    assert manager.get_window_state("@9").session_id == "rollout-id"
    assert manager.get_active_session(42) is sess
    data = json.loads(config.session_map_file.read_text())
    assert data["ccbot:@9"]["transcript_path"] == str(transcript)
