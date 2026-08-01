from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "scripts" / "staging_import_archives.py"
    spec = importlib.util.spec_from_file_location("staging_import_archives", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rollout(root: Path, rollout_id: str, *, incomplete_tail: bool = False) -> Path:
    path = root / "sessions" / "2026" / "08" / "01" / f"rollout-{rollout_id}.jsonl"
    path.parent.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"id": rollout_id, "cwd": "/prod"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "q"}},
        {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "FINAL_MARKER"},
        },
    ]
    content = "".join(json.dumps(row) + "\n" for row in rows)
    if incomplete_tail:
        content += '{"type":"event_msg"'
    path.write_text(content)
    return path


def _state(path: Path, *, state: str = "archived") -> None:
    data = {
        "sessions": {
            "deadbeef": {
                "id": "deadbeef",
                "name": "heavy",
                "window_id": "@9" if state == "active" else "",
                "workdir": "/prod",
                "state": state,
                "claude_session_id": "rollout-1",
                "backend": "codex",
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_import_snapshots_copies_only_complete_jsonl_and_safe_workdir(tmp_path):
    module = _module()
    prod_state = tmp_path / "prod" / "state.json"
    prod_codex = tmp_path / "prod-codex"
    staging = tmp_path / "staging"
    staging_codex = tmp_path / "staging-codex"
    _state(prod_state)
    _rollout(prod_codex, "rollout-1", incomplete_tail=True)

    imported = module.import_snapshots(
        session_ids=["deadbeef"],
        names=[],
        allow_live=False,
        production_state=prod_state,
        production_codex_home=prod_codex,
        staging_dir=staging,
        staging_codex_home=staging_codex,
    )

    assert imported[0][0] == "deadbeef"
    state = json.loads((staging / "state.json").read_text())
    record = state["sessions"]["deadbeef"]
    assert record["state"] == "archived"
    assert record["window_id"] == ""
    assert record["workdir"] == str(staging / "workspaces" / "imported" / "deadbeef")
    copied = next((staging_codex / "sessions").rglob("*.jsonl"))
    assert "FINAL_MARKER" in copied.read_text()
    assert copied.read_bytes().endswith(b"\n")
    assert b'{"type":"event_msg"' not in copied.read_bytes().splitlines()[-1]


def test_live_snapshot_requires_explicit_flag(tmp_path):
    module = _module()
    prod_state = tmp_path / "prod" / "state.json"
    prod_codex = tmp_path / "prod-codex"
    _state(prod_state, state="active")
    _rollout(prod_codex, "rollout-1")

    with pytest.raises(RuntimeError, match="allow-live-snapshot"):
        module.import_snapshots(
            session_ids=["deadbeef"],
            names=[],
            allow_live=False,
            production_state=prod_state,
            production_codex_home=prod_codex,
            staging_dir=tmp_path / "staging",
            staging_codex_home=tmp_path / "staging-codex",
        )
