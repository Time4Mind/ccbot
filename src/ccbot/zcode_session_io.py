"""Idempotent, CCBot-owned journals over read-only native ZCode sessions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import fcntl
import json
import logging
from pathlib import Path
from typing import Any

from .session_models import ClaudeSession
from .transcript_zcode import project_snapshot
from .utils import ccbot_dir
from .zcode_store import (
    SESSION_ID,
    ZCodeStoreError,
    database_path,
    list_sessions,
    read_snapshot,
)

logger = logging.getLogger(__name__)


@dataclass
class _Cursor:
    seen: set[str] = field(default_factory=set)
    watermark: int = 0
    size: int = 0


_cursors: dict[Path, _Cursor] = {}


def journal_path(session_id: str) -> Path:
    if not SESSION_ID.fullmatch(session_id):
        raise ZCodeStoreError("Invalid ZCode session identity")
    return ccbot_dir() / "zcode-transcripts" / f"{session_id}.jsonl"


def _header(path: Path) -> dict[str, Any]:
    try:
        with path.open() as handle:
            data = json.loads(handle.readline())
        return data if data.get("type") == "zcode_meta" else {}
    except (OSError, ValueError, AttributeError):
        return {}


def session_id_for_path(path: Path) -> str:
    return str(_header(path).get("sessionId") or "")


def ensure_projection(session_id: str, cwd: str, source: Path | None = None) -> Path:
    path = journal_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists() or path.stat().st_size == 0:
            data = {
                "type": "zcode_meta",
                "sessionId": session_id,
                "cwd": str(Path(cwd).resolve()),
                "database_path": str((source or database_path()).resolve()),
            }
            with path.open("w") as handle:
                handle.write(json.dumps(data, ensure_ascii=False) + "\n")
            path.chmod(0o600)
        header = _header(path)
        if (
            header.get("sessionId") != session_id
            or Path(header.get("cwd", "")).resolve() != Path(cwd).resolve()
        ):
            raise ZCodeStoreError("ZCode journal identity does not match session")
    return path


def refresh_transcript(
    session_id: str, cwd: str, source: Path | None = None
) -> Path | None:
    path = journal_path(session_id)
    header = _header(path)
    source = source or (
        Path(header["database_path"])
        if header.get("database_path")
        else database_path()
    )
    if not source.is_file():
        return (
            path
            if header.get("sessionId") == session_id
            and (not cwd or header.get("cwd") == str(Path(cwd).resolve()))
            else None
        )
    if not cwd:
        cwd = str(header.get("cwd") or "")
    if not header:
        snapshot = read_snapshot(session_id, cwd, path=source)
        if snapshot is None:
            return None
        cwd = snapshot["session"]["directory"]
    path = ensure_projection(session_id, cwd, source)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cursor = _cursors.setdefault(path, _Cursor())
        size = path.stat().st_size
        if size != cursor.size:
            cursor = _Cursor()
            # A crash can interrupt the last append. Repair only our journal,
            # never the native SQLite store or another backend's transcript.
            with path.open("r+b") as handle:
                good = 0
                for line in handle:
                    if not line.endswith(b"\n"):
                        handle.truncate(good)
                        break
                    try:
                        row = json.loads(line)
                    except ValueError as exc:
                        raise ZCodeStoreError("Invalid ZCode journal record") from exc
                    if row.get("uuid"):
                        cursor.seen.add(row["uuid"])
                    good = handle.tell()
            _cursors[path] = cursor
        snapshot = read_snapshot(session_id, cwd, path=source, since=cursor.watermark)
        if snapshot is None:
            return None
        rows = [
            row for row in project_snapshot(snapshot) if row["uuid"] not in cursor.seen
        ]
        if rows:
            with path.open("a") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            cursor.seen.update(row["uuid"] for row in rows)
        cursor.watermark = snapshot["watermark"]
        cursor.size = path.stat().st_size
    return path


def build_session_file_path(session_id: str, cwd: str = "") -> Path | None:
    try:
        return refresh_transcript(session_id, cwd)
    except (ZCodeStoreError, OSError, ValueError) as exc:
        logger.warning("ZCode transcript unavailable: %s", exc)
        return None


def _metadata(path: Path, session_id: str) -> ClaudeSession:
    count = 0
    summary = "Untitled"
    tokens = 0
    with path.open() as handle:
        for line in handle:
            data = json.loads(line)
            if data.get("type") == "zcode_usage":
                usage = data["usage"]
                tokens += int(usage.get("computed_total_tokens") or 0)
            elif data.get("type") in ("user", "assistant"):
                count += 1
                if data["type"] == "user":
                    texts = [
                        part["text"]
                        for part in data["message"]["content"]
                        if part.get("type") == "text"
                    ]
                    if texts:
                        summary = " ".join(" ".join(texts).split())[:50]
    return ClaudeSession(
        session_id=session_id,
        summary=summary,
        message_count=count,
        file_path=str(path),
        token_total=tokens,
    )


async def get_session_direct(
    session_id: str, cwd: str, transcript_path: str | None = None
) -> ClaudeSession | None:
    def load() -> ClaudeSession | None:
        if transcript_path and session_id_for_path(Path(transcript_path)) != session_id:
            return None
        path = build_session_file_path(session_id, cwd)
        return _metadata(path, session_id) if path else None

    return await asyncio.to_thread(load)


async def list_sessions_for_directory(cwd: str) -> list[ClaudeSession]:
    def load() -> list[ClaudeSession]:
        try:
            result = []
            for session in list_sessions(cwd):
                path = build_session_file_path(session["id"], cwd)
                if path:
                    result.append(_metadata(path, session["id"]))
            return result[:10]
        except (ZCodeStoreError, OSError):
            return []

    return await asyncio.to_thread(load)


async def scan_active_sessions(cwds: set[str]) -> list[tuple[str, Path]]:
    result = []
    for cwd in cwds:
        for session in await list_sessions_for_directory(cwd):
            result.append((session.session_id, Path(session.file_path)))
    return result


def reported_usage(path: Path) -> dict[str, Any]:
    usage: dict[str, dict[str, Any]] = {}
    with path.open() as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("type") == "zcode_usage":
                usage[row["usage"]["id"]] = row["usage"]
    records = list(usage.values())
    latest = max(records, key=lambda item: int(item.get("started_at") or 0), default={})
    return {
        "input_tokens": sum(int(item.get("input_tokens") or 0) for item in records),
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in records),
        "total_tokens": sum(
            int(item.get("computed_total_tokens") or 0) for item in records
        ),
        "model": latest.get("model_id"),
        "context_tokens": latest.get("input_tokens"),
        # The native usage store reports tokens but no context capacity.
        "context_window": None,
    }


async def context_fill(path: Path) -> tuple[int, int] | None:
    data = await asyncio.to_thread(reported_usage, path)
    if data["context_tokens"] is None or not data["context_window"]:
        return None
    return int(data["context_tokens"]), int(data["context_window"])
