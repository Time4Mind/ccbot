from __future__ import annotations

import asyncio
import json

import pytest

from ccbot.config import config
from ccbot.session import SessionManager
from ccbot.session_map_store import remove_session_map_entries, upsert_session_map_entry
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


def test_remove_session_map_entries_purges_every_duplicate(tmp_path):
    map_file = tmp_path / "session_map.json"
    map_file.write_text(
        json.dumps(
            {
                "ccbot:@1": {"session_id": "target", "backend": "codex"},
                "ccbot:@2": {"session_id": "keep", "backend": "codex"},
                "ccbot:@3": {"session_id": "target", "backend": "codex"},
                "ccbot:@4": {"hook_only": True},
            }
        )
    )

    removed = remove_session_map_entries(map_file, "target")

    assert removed == ["ccbot:@1", "ccbot:@3"]
    data = json.loads(map_file.read_text())
    assert set(data) == {"ccbot:@2", "ccbot:@4"}


@pytest.mark.asyncio
async def test_remove_provider_bindings_clears_memory_and_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
    monkeypatch.setattr(config, "tmux_session_name", "ccbot")
    config.session_map_file.write_text(
        json.dumps(
            {
                "ccbot:@7": {"session_id": "provider-id"},
                "ccbot:@9": {"session_id": "provider-id"},
            }
        )
    )
    manager = SessionManager()
    monkeypatch.setattr(manager, "save_state", lambda: None)
    manager.get_window_state("@7").session_id = "provider-id"
    manager.get_window_state("@9").session_id = "provider-id"
    manager.window_display_names.update({"@7": "one", "@9": "two"})

    removed = await manager.remove_provider_session_bindings("provider-id")

    assert removed == {"@7", "@9"}
    assert not manager.window_states
    assert not manager.window_display_names
    assert json.loads(config.session_map_file.read_text()) == {}


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
