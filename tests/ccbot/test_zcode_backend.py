"""ZCode store, journal, history and lifecycle contracts using native-shaped rows."""

import hashlib
import json
import sqlite3

import pytest

from ccbot import zcode_session_io as io
from ccbot.transcript_parser import TranscriptParser
from ccbot.zcode_store import ZCodeStoreError, open_store, read_snapshot

SID = "sess_12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "native.sqlite"
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path / "bot"))
    monkeypatch.setenv("ZCODE_SESSION_DB_PATH", str(path))
    io._cursors.clear()
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, title TEXT,
            parent_id TEXT, time_updated INTEGER, time_deleted INTEGER);
        CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT,
            time_created INTEGER, time_updated INTEGER, sequence INTEGER);
        CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            data TEXT, time_created INTEGER, time_updated INTEGER, sequence INTEGER);
        CREATE TABLE model_usage(id TEXT PRIMARY KEY, session_id TEXT,
            assistant_message_id TEXT, started_at INTEGER, status TEXT,
            model_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
            computed_total_tokens INTEGER, cache_read_input_tokens INTEGER);
    """)
    db.execute(
        "INSERT INTO session VALUES (?, ?, 'Test', NULL, 100, NULL)",
        (SID, str(tmp_path)),
    )
    db.commit()
    yield db, path, tmp_path
    db.close()
    io._cursors.clear()


def message(db, identity, role, text, *, completed=True, synthetic=False, sequence=0):
    data = {"role": role, "time": {"created": 100}, "modelId": "glm-4.5-flash"}
    if role == "assistant" and completed:
        data["time"]["completed"] = 200
        data["finish"] = "stop"
    if synthetic:
        data["semantics"] = {"uiVisibility": "hidden", "transcriptVisibility": "hidden"}
    db.execute(
        "INSERT INTO message VALUES (?, ?, ?, 100, 200, ?)",
        (identity, SID, json.dumps(data), sequence),
    )
    db.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, 100, 200, 0)",
        (identity + "-part", identity, SID, json.dumps({"type": "text", "text": text})),
    )
    db.commit()


def entries(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_read_only_store_and_wrong_directory(store):
    _db, path, cwd = store
    before = hashlib.sha256(path.read_bytes()).digest()
    snapshot = read_snapshot(SID, str(cwd))
    assert snapshot and snapshot["session"]["id"] == SID
    assert read_snapshot(SID, str(cwd / "other")) is None
    with pytest.raises(ZCodeStoreError):
        with open_store() as reader:
            reader.execute("DELETE FROM session")
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_schema_mismatch_fails_without_creating_database(tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ZCodeStoreError):
        with open_store(missing):
            pass
    assert not missing.exists()
    db = sqlite3.connect(missing)
    db.close()
    with pytest.raises(ZCodeStoreError, match="schema"):
        with open_store(missing):
            pass


def test_visible_history_idempotent_restart_and_partial_tail(store):
    db, _path, cwd = store
    message(db, "hidden", "user", "SYSTEM MUST NEVER APPEAR", synthetic=True)
    message(db, "user", "user", "Question", sequence=1)
    message(db, "answer", "assistant", "Answer", sequence=2)
    path = io.build_session_file_path(SID, str(cwd))
    assert path
    original = path.read_bytes()
    assert b"SYSTEM MUST NEVER APPEAR" not in original
    assert io.build_session_file_path(SID, str(cwd)) == path
    assert path.read_bytes() == original
    io._cursors.clear()
    assert io.build_session_file_path(SID, str(cwd)) == path
    assert path.read_bytes() == original
    with path.open("ab") as handle:
        handle.write(b'{"incomplete":')
    io._cursors.clear()
    assert io.build_session_file_path(SID, str(cwd)) == path
    assert path.read_bytes() == original
    parsed, _ = TranscriptParser.parse_entries(entries(path))
    assert [(p.role, p.text) for p in parsed] == [
        ("user", "Question"),
        ("assistant", "Answer"),
    ]
    assert parsed[-1].stop_reason == "end_turn"


def test_running_tool_then_result_pairs_once(store):
    db, _path, cwd = store
    message(db, "assistant", "assistant", "", completed=False)
    state = {
        "status": "running",
        "input": {"file_path": "example.txt"},
        "time": {"start": 200},
    }
    part = {"type": "tool", "callID": "call1", "tool": "Read", "state": state}
    db.execute(
        "INSERT INTO part VALUES ('tool', 'assistant', ?, ?, 200, 201, 1)",
        (SID, json.dumps(part)),
    )
    db.commit()
    path = io.build_session_file_path(SID, str(cwd))
    assert path
    parsed, pending = TranscriptParser.parse_entries(entries(path), pending_tools={})
    assert [p.content_type for p in parsed] == ["tool_use"]
    assert "call1" in pending
    offset = path.stat().st_size
    state.update(
        status="completed", output="file contents", time={"start": 200, "end": 300}
    )
    db.execute(
        "UPDATE part SET data=?, time_updated=300 WHERE id='tool'", (json.dumps(part),)
    )
    db.commit()
    io.build_session_file_path(SID, str(cwd))
    with path.open("rb") as handle:
        handle.seek(offset)
        new = [json.loads(line) for line in handle]
    parsed, pending = TranscriptParser.parse_entries(new, pending_tools=pending)
    assert [p.content_type for p in parsed] == ["tool_result"]
    assert "Read 1 lines" in parsed[0].text
    assert new[0]["message"]["content"][0]["content"][0]["text"] == "file contents"
    assert not pending
    assert sum(row.get("uuid") == "tool:call" for row in entries(path)) == 1


@pytest.mark.asyncio
async def test_usage_and_directory_metadata(store):
    db, _path, cwd = store
    message(db, "u", "user", "User question")
    message(db, "a", "assistant", "Response", sequence=1)
    db.execute(
        "INSERT INTO model_usage VALUES ('usage1', ?, 'a', 100, 'completed', 'glm-4.5-flash', 13107, 50, 13157, 10000)",
        (SID,),
    )
    db.commit()
    path = io.build_session_file_path(SID, str(cwd))
    assert path
    assert io.reported_usage(path)["total_tokens"] == 13157
    assert io.reported_usage(path)["context_tokens"] == 13107
    # Native usage has no context capacity; model names are not evidence.
    assert io.reported_usage(path)["context_window"] is None
    assert await io.context_fill(path) is None
    info = await io.get_session_direct(SID, str(cwd))
    assert info and info.token_total == 13157 and info.summary == "User question"
    assert [
        item.session_id for item in await io.list_sessions_for_directory(str(cwd))
    ] == [SID]


def test_structured_auth_error_and_no_prose_detection(store):
    db, _path, cwd = store
    message(db, "ordinary", "assistant", "We discussed authentication_failed")
    data = {
        "role": "assistant",
        "time": {"created": 100, "completed": 200},
        "error": {
            "name": "APIError",
            "data": {"statusCode": 401, "message": "Invalid key"},
        },
    }
    db.execute(
        "INSERT INTO message VALUES ('error', ?, ?, 100, 200, 1)",
        (SID, json.dumps(data)),
    )
    db.commit()
    path = io.build_session_file_path(SID, str(cwd))
    assert path
    parsed, _ = TranscriptParser.parse_entries(entries(path))
    assert parsed[0].api_error == ""
    assert parsed[-1].api_error == "authentication_failed"


def test_identity_validation_rejects_path_traversal(store):
    _db, _path, cwd = store
    assert io.build_session_file_path("../../other", str(cwd)) is None
    assert io.build_session_file_path(SID, str(cwd / "other")) is None


@pytest.mark.asyncio
async def test_monitor_refreshes_native_store_with_direct_transcript_target(store):
    from ccbot.session_monitor import SessionInfo, SessionMonitor

    db, _source, cwd = store
    path = io.build_session_file_path(SID, str(cwd))
    assert path
    monitor = SessionMonitor(
        projects_path=cwd / "projects", state_file=cwd / "monitor.json"
    )
    targets = [SessionInfo(session_id=SID, file_path=path)]
    assert await monitor.check_for_updates({SID}, session_infos=targets) == []
    message(db, "live-answer", "assistant", "DIRECT_TARGET_FINAL")
    updates = await monitor.check_for_updates({SID}, session_infos=targets)
    assert [(item.text, item.stop_reason) for item in updates] == [
        ("DIRECT_TARGET_FINAL", "end_turn")
    ]
    assert await monitor.check_for_updates({SID}, session_infos=targets) == []
