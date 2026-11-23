"""Root conftest — sets env vars BEFORE any ccbot module is imported.

The config.py module-level singleton requires TELEGRAM_BOT_TOKEN and
ALLOWED_USERS at import time, so these must be set before pytest
discovers any test that transitively imports ccbot.
"""

import os
import tempfile
import sqlite3

import pytest

# Force-set (not setdefault) to prevent real env vars from leaking into tests
os.environ["TELEGRAM_BOT_TOKEN"] = "test:0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ALLOWED_USERS"] = "12345"
os.environ["CCBOT_DIR"] = tempfile.mkdtemp(prefix="ccbot-test-")


@pytest.fixture
def opencode_native(tmp_path, monkeypatch):
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
        ("ses_ccbotAcceptance123", str(tmp_path)),
    )
    db.commit()
    yield db, path, tmp_path
    db.close()
