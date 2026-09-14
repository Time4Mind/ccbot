"""Persistence keeps cold summary data out of frequent hot-state writes."""

import json

from ccbot.config import config
from ccbot.session import SessionManager


def test_summary_cache_round_trips_through_independent_cold_file(
    tmp_path, monkeypatch
) -> None:
    state_file = tmp_path / "state.json"
    summary_file = tmp_path / "summary_cache.json"
    monkeypatch.setattr(config, "state_file", state_file)
    monkeypatch.setattr(config, "summary_cache_file", summary_file, raising=False)

    manager = SessionManager()
    manager.summary_cache["session-1"] = {
        "summary": "cold description" * 100,
        "mtime": 12.0,
        "ts": 13.0,
    }
    manager.active_sessions[7] = "session-1"
    manager.save_state()

    hot_state = json.loads(state_file.read_text())
    assert "summary_cache" not in hot_state
    assert json.loads(summary_file.read_text()) == manager.summary_cache

    restored = SessionManager()
    assert restored.summary_cache == manager.summary_cache
    assert restored.active_sessions == {7: "session-1"}


def test_unchanged_summary_cache_is_not_rewritten_with_hot_state(
    tmp_path, monkeypatch
) -> None:
    state_file = tmp_path / "state.json"
    summary_file = tmp_path / "summary_cache.json"
    monkeypatch.setattr(config, "state_file", state_file)
    monkeypatch.setattr(config, "summary_cache_file", summary_file, raising=False)

    manager = SessionManager()
    manager.summary_cache["session-1"] = {"summary": "stable"}
    manager.save_state()
    cold_inode = summary_file.stat().st_ino

    manager.active_sessions[7] = "session-1"
    manager.save_state()

    assert summary_file.stat().st_ino == cold_inode
    assert json.loads(state_file.read_text())["active_sessions"] == {"7": "session-1"}


def test_unchanged_hot_state_is_not_rewritten(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(config, "state_file", state_file)
    monkeypatch.setattr(
        config, "summary_cache_file", tmp_path / "summary_cache.json", raising=False
    )
    manager = SessionManager()

    manager.active_sessions[42] = "same"
    manager.save_state()
    state_inode = state_file.stat().st_ino

    manager.save_state()

    assert state_file.stat().st_ino == state_inode


def test_legacy_embedded_summary_cache_migrates_without_data_loss(
    tmp_path, monkeypatch
) -> None:
    state_file = tmp_path / "state.json"
    summary_file = tmp_path / "summary_cache.json"
    state_file.write_text(
        json.dumps({"summary_cache": {"legacy": {"summary": "kept"}}})
    )
    monkeypatch.setattr(config, "state_file", state_file)
    monkeypatch.setattr(config, "summary_cache_file", summary_file, raising=False)

    manager = SessionManager()
    assert manager.summary_cache == {"legacy": {"summary": "kept"}}

    manager.save_state()

    assert "summary_cache" not in json.loads(state_file.read_text())
    assert json.loads(summary_file.read_text()) == manager.summary_cache
