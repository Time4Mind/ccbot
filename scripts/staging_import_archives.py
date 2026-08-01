#!/usr/bin/env python3
"""Snapshot selected production Codex sessions into the stopped staging bot."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

PRODUCTION_STATE = Path("/Users/a-s-nosko/.ccbot/state.json")
PRODUCTION_CODEX_HOME = Path("/Users/a-s-nosko/.codex")
STAGING_DIR = Path("/Users/a-s-nosko/.ccbot-staging")
STAGING_CODEX_HOME = Path("/Users/a-s-nosko/.codex-staging")
STAGING_WORKSPACES = STAGING_DIR / "workspaces" / "imported"


def _atomic_write_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, value: Any) -> None:
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
    _atomic_write_bytes(path, payload)


def _session_meta_id(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for _ in range(16):
            line = handle.readline()
            if not line:
                break
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "session_meta":
                payload = entry.get("payload")
                return str(payload.get("id") or "") if isinstance(payload, dict) else ""
    return ""


def _find_rollout(session_id: str, codex_home: Path) -> Path:
    root = codex_home / "sessions"
    candidates = sorted(root.glob(f"*/*/*/rollout-*{session_id}.jsonl"))
    matches = [path for path in candidates if _session_meta_id(path) == session_id]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one rollout for {session_id}, found {len(matches)}"
        )
    return matches[0]


def _snapshot_complete_jsonl(source: Path, destination: Path) -> int:
    raw = source.read_bytes()
    newline = raw.rfind(b"\n")
    if newline < 0:
        raise RuntimeError(f"rollout has no complete JSONL records: {source}")
    snapshot = raw[: newline + 1]
    for line_no, line in enumerate(snapshot.splitlines(), start=1):
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSONL at line {line_no}: {source}") from exc
    _atomic_write_bytes(destination, snapshot)
    return len(snapshot)


def _selected_sessions(
    state: dict[str, Any], session_ids: list[str], names: list[str]
) -> list[dict[str, Any]]:
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        raise RuntimeError("production state has no sessions object")
    selected: dict[str, dict[str, Any]] = {}
    for session_id in session_ids:
        record = sessions.get(session_id)
        if not isinstance(record, dict):
            raise RuntimeError(f"unknown production session id: {session_id}")
        selected[session_id] = dict(record)
    for wanted_name in names:
        matches = [
            dict(record)
            for record in sessions.values()
            if isinstance(record, dict)
            and str(record.get("name") or "").casefold() == wanted_name.casefold()
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected one production session named {wanted_name!r}, "
                f"found {len(matches)}"
            )
        selected[str(matches[0].get("id") or "")] = matches[0]
    if not selected:
        raise RuntimeError("select at least one session with --session-id or --name")
    return list(selected.values())


def import_snapshots(
    *,
    session_ids: list[str],
    names: list[str],
    allow_live: bool,
    production_state: Path = PRODUCTION_STATE,
    production_codex_home: Path = PRODUCTION_CODEX_HOME,
    staging_dir: Path = STAGING_DIR,
    staging_codex_home: Path = STAGING_CODEX_HOME,
) -> list[tuple[str, str, int]]:
    if staging_dir.resolve() == Path("/Users/a-s-nosko/.ccbot").resolve():
        raise RuntimeError("staging dir resolves to production")
    if staging_codex_home.resolve() == Path("/Users/a-s-nosko/.codex").resolve():
        raise RuntimeError("staging CODEX_HOME resolves to production")

    lock_path = staging_dir / "ccbot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("staging bot must be stopped before import") from exc

        source_state = json.loads(production_state.read_text())
        selected = _selected_sessions(source_state, session_ids, names)
        staging_state_path = staging_dir / "state.json"
        staging_state = (
            json.loads(staging_state_path.read_text())
            if staging_state_path.exists()
            else {
                "window_states": {},
                "user_window_offsets": {},
                "active_sessions": {},
                "active_history": {},
                "sessions": {},
                "last_switcher_msg_id": {},
                "card_msg_id": {},
                "window_display_names": {},
                "user_settings": {},
                "agent_backend": "codex",
                "summary_cache": {},
                "bg_status": {},
            }
        )
        staging_sessions = staging_state.setdefault("sessions", {})
        imported: list[tuple[str, str, int]] = []

        for source_record in selected:
            session_id = str(source_record.get("id") or "")
            rollout_id = str(source_record.get("claude_session_id") or "")
            backend = str(source_record.get("backend") or "")
            source_status = str(source_record.get("state") or "")
            if not session_id or not rollout_id:
                raise RuntimeError("selected session has no session or rollout id")
            if backend != "codex":
                raise RuntimeError(f"{session_id} is {backend}, not codex")
            if source_status in ("active", "idle") and not allow_live:
                raise RuntimeError(
                    f"{session_id} is live; pass --allow-live-snapshot explicitly"
                )

            existing = staging_sessions.get(session_id)
            if isinstance(existing, dict) and existing.get("state") in (
                "active",
                "idle",
            ):
                raise RuntimeError(f"staging session {session_id} is currently live")

            source_rollout = _find_rollout(rollout_id, production_codex_home)
            relative = source_rollout.relative_to(production_codex_home / "sessions")
            destination = staging_codex_home / "sessions" / relative
            copied_bytes = _snapshot_complete_jsonl(source_rollout, destination)
            if _session_meta_id(destination) != rollout_id:
                raise RuntimeError(f"copied rollout id mismatch for {session_id}")

            safe_workdir = staging_dir / "workspaces" / "imported" / session_id
            safe_workdir.mkdir(parents=True, exist_ok=True)
            record = dict(source_record)
            record.update(
                {
                    "window_id": "",
                    "workdir": str(safe_workdir),
                    "state": "archived",
                    "archived_at": time.time(),
                    "backend": "codex",
                    "was_lost": False,
                }
            )
            staging_sessions[session_id] = record
            imported.append((session_id, str(record.get("name") or ""), copied_bytes))

        staging_state["agent_backend"] = "codex"
        _atomic_write_json(staging_state_path, staging_state)
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        return imported


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", action="append", default=[])
    parser.add_argument("--name", action="append", default=[])
    parser.add_argument("--allow-live-snapshot", action="store_true")
    args = parser.parse_args()
    imported = import_snapshots(
        session_ids=args.session_id,
        names=args.name,
        allow_live=args.allow_live_snapshot,
    )
    for session_id, name, copied_bytes in imported:
        print(f"imported {session_id} {name!r} ({copied_bytes} bytes)")


if __name__ == "__main__":
    main()
