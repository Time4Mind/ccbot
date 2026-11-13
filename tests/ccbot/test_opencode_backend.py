"""Native OpenCode sessions must retain their backend, history and tool boundaries."""

import hashlib
import json
import sqlite3

import pytest

from ccbot.agent_backends import session_transcript_path
from ccbot.transcript_parser import TranscriptParser

SID = "ses_ccbotAcceptance123"


@pytest.fixture
def native(tmp_path, monkeypatch):
    path = tmp_path / "opencode.db"
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path / "ccbot"))
    monkeypatch.setenv("OPENCODE_SESSION_DB_PATH", str(path))
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, title TEXT,
            parent_id TEXT, time_created INTEGER, time_updated INTEGER,
            time_archived INTEGER);
        CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
    """)
    db.execute(
        "INSERT INTO session VALUES (?, ?, 'Test', NULL, 100, 100, NULL)",
        (SID, str(tmp_path)),
    )
    db.commit()
    yield db, path, tmp_path
    db.close()


def add_message(db, identity, role, text, *, hidden=False, completed=True):
    data = {"role": role, "time": {"created": 100}, "modelID": "gpt-6-luna"}
    if role == "assistant" and completed:
        data.update(
            finish="stop",
            tokens={
                "input": 100,
                "output": 20,
                "reasoning": 3,
                "cache": {"read": 40, "write": 0},
            },
        )
        data["time"]["completed"] = 200
    db.execute(
        "INSERT INTO message VALUES (?, ?, 100, 200, ?)",
        (identity, SID, json.dumps(data)),
    )
    db.execute(
        "INSERT INTO part VALUES (?, ?, ?, 100, 200, ?)",
        (
            identity + "-text",
            identity,
            SID,
            json.dumps({"type": "text", "text": text, "synthetic": hidden}),
        ),
    )
    db.commit()


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_opencode_history_is_read_only_and_idempotent(native):
    db, source, cwd = native
    add_message(db, "u1", "user", "Question")
    add_message(db, "hidden", "user", "Hidden harness", hidden=True)
    add_message(db, "a1", "assistant", "Answer")
    before = hashlib.sha256(source.read_bytes()).digest()
    path = session_transcript_path(SID, str(cwd), "opencode")
    assert path and path.is_file()
    parsed, _ = TranscriptParser.parse_entries(rows(path))
    assert [(p.role, p.text) for p in parsed] == [
        ("user", "Question"),
        ("assistant", "Answer"),
    ]
    assert parsed[-1].stop_reason == "end_turn"
    first = path.read_bytes()
    assert session_transcript_path(SID, str(cwd), "opencode") == path
    assert path.read_bytes() == first
    assert hashlib.sha256(source.read_bytes()).digest() == before
    assert session_transcript_path(SID, str(cwd / "other"), "opencode") is None
    assert session_transcript_path("../../unsafe", str(cwd), "opencode") is None


def test_running_tool_becomes_one_paired_result(native):
    db, _source, cwd = native
    add_message(db, "a1", "assistant", "", completed=False)
    tool = {
        "type": "tool",
        "tool": "read",
        "callID": "call-1",
        "state": {"status": "running", "input": {"filePath": "test.txt"}},
    }
    db.execute(
        "INSERT INTO part VALUES ('tool', 'a1', ?, 100, 200, ?)",
        (SID, json.dumps(tool)),
    )
    db.commit()
    path = session_transcript_path(SID, str(cwd), "opencode")
    assert path
    parsed, pending = TranscriptParser.parse_entries(rows(path), pending_tools={})
    assert [p.content_type for p in parsed] == ["tool_use"]
    offset = path.stat().st_size
    tool["state"].update(status="completed", output="file contents")
    db.execute(
        "UPDATE part SET data=?, time_updated=300 WHERE id='tool'", (json.dumps(tool),)
    )
    db.commit()
    session_transcript_path(SID, str(cwd), "opencode")
    with path.open("rb") as handle:
        handle.seek(offset)
        tail = [json.loads(line) for line in handle]
    parsed, pending = TranscriptParser.parse_entries(tail, pending_tools=pending)
    assert [p.content_type for p in parsed] == ["tool_result"]
    assert not pending
    assert sum(row.get("uuid") == "tool:call" for row in rows(path)) == 1


def test_restart_repairs_only_owned_journal_and_preserves_native_store(native):
    from ccbot import opencode_session_io as io

    db, source, cwd = native
    add_message(db, "u1", "user", "Question")
    path = session_transcript_path(SID, str(cwd), "opencode")
    assert path
    good = path.read_bytes()
    source_bytes = source.read_bytes()
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    io._cursors.clear()
    assert session_transcript_path(SID, str(cwd), "opencode") == path
    assert path.read_bytes() == good
    assert source.read_bytes() == source_bytes


def test_missing_or_incompatible_store_is_not_created_or_migrated(tmp_path):
    from ccbot.opencode_store import OpenCodeStoreError, open_store

    path = tmp_path / "missing.db"
    with pytest.raises(OpenCodeStoreError):
        with open_store(path):
            pass
    assert not path.exists()
    sqlite3.connect(path).close()
    before = path.read_bytes()
    with pytest.raises(OpenCodeStoreError, match="schema"):
        with open_store(path):
            pass
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_live_monitor_refreshes_opencode_projection(native):
    from ccbot.session_monitor import SessionInfo, SessionMonitor

    db, _source, cwd = native
    path = session_transcript_path(SID, str(cwd), "opencode")
    assert path
    monitor = SessionMonitor(
        projects_path=cwd / "projects", state_file=cwd / "monitor.json"
    )
    targets = [SessionInfo(session_id=SID, file_path=path)]
    assert await monitor.check_for_updates({SID}, session_infos=targets) == []
    add_message(db, "a1", "assistant", "OPENCODE_FINAL")
    updates = await monitor.check_for_updates({SID}, session_infos=targets)
    assert [(item.text, item.stop_reason) for item in updates] == [
        ("OPENCODE_FINAL", "end_turn")
    ]
    assert await monitor.check_for_updates({SID}, session_infos=targets) == []
