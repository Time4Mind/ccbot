"""Read-only discovery and metadata extraction for Codex CLI rollouts.

Codex stores sessions below ``$CODEX_HOME/sessions/YYYY/MM/DD`` as
``rollout-*.jsonl``.  This module exposes the same small interface as
``session_claude_io`` so the existing picker, history, and card layers can
remain backend-neutral.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .config import config
from .session_models import ClaudeSession


def _read_meta(path: Path) -> dict[str, Any]:
    """Return the first Codex ``session_meta`` payload in a rollout."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(8):
                line = handle.readline()
                if not line:
                    break
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "session_meta":
                    payload = data.get("payload")
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        pass
    return {}


def _parse_rollout(path: Path, expected_id: str = "") -> ClaudeSession | None:
    meta = _read_meta(path)
    session_id = str(meta.get("id") or expected_id)
    if not session_id:
        return None
    last_user = ""
    message_count = 0
    token_total = 0
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in raw.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = data.get("payload")
        if not isinstance(payload, dict):
            continue
        if data.get("type") == "event_msg":
            ptype = payload.get("type")
            if ptype in ("user_message", "agent_message"):
                message_count += 1
            if ptype == "user_message":
                last_user = str(payload.get("message") or "").strip() or last_user
            elif ptype == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    usage = info.get("total_token_usage")
                    if isinstance(usage, dict):
                        token_total = int(usage.get("total_tokens", token_total) or 0)
    summary = last_user[:50] if last_user else "Untitled"
    return ClaudeSession(
        session_id=session_id,
        summary=summary,
        message_count=message_count,
        file_path=str(path),
        token_total=token_total,
    )


def _rollouts() -> list[Path]:
    root = config.codex_sessions_path
    if not root.is_dir():
        return []
    try:
        return sorted(
            root.glob("*/*/*/rollout-*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []


def build_session_file_path(session_id: str, cwd: str = "") -> Path | None:
    """Resolve a rollout path by the authoritative id in ``session_meta``."""
    for path in _rollouts():
        meta = _read_meta(path)
        if str(meta.get("id") or "") == session_id:
            return path
    return None


async def get_session_direct(session_id: str, cwd: str) -> ClaudeSession | None:
    """Find a Codex rollout by metadata id, optionally verifying its cwd."""
    wanted_cwd = str(Path(cwd).expanduser().resolve()) if cwd else ""
    for path in await asyncio.to_thread(_rollouts):
        meta = await asyncio.to_thread(_read_meta, path)
        if str(meta.get("id") or "") != session_id:
            continue
        meta_cwd = str(meta.get("cwd") or "")
        if wanted_cwd and meta_cwd:
            try:
                if str(Path(meta_cwd).expanduser().resolve()) != wanted_cwd:
                    continue
            except (OSError, ValueError):
                if meta_cwd != cwd:
                    continue
        return await asyncio.to_thread(_parse_rollout, path, session_id)
    return None


async def list_sessions_for_directory(cwd: str) -> list[ClaudeSession]:
    """Return the ten newest Codex sessions whose metadata cwd matches."""
    try:
        wanted = str(Path(cwd).expanduser().resolve())
    except (OSError, ValueError):
        wanted = cwd
    matches: list[ClaudeSession] = []
    for path in await asyncio.to_thread(_rollouts):
        meta = await asyncio.to_thread(_read_meta, path)
        meta_cwd = str(meta.get("cwd") or "")
        try:
            meta_cwd = str(Path(meta_cwd).expanduser().resolve())
        except (OSError, ValueError):
            pass
        if meta_cwd != wanted:
            continue
        parsed = await asyncio.to_thread(_parse_rollout, path)
        if parsed and parsed.message_count > 0:
            matches.append(parsed)
        if len(matches) >= 10:
            break
    return matches


async def scan_active_sessions(active_cwds: set[str]) -> list[tuple[str, Path]]:
    """Return ``(session_id, rollout_path)`` pairs for active working dirs."""
    out: list[tuple[str, Path]] = []
    for path in await asyncio.to_thread(_rollouts):
        meta = await asyncio.to_thread(_read_meta, path)
        sid = str(meta.get("id") or "")
        cwd = str(meta.get("cwd") or "")
        try:
            cwd = str(Path(cwd).expanduser().resolve())
        except (OSError, ValueError):
            pass
        if sid and cwd in active_cwds:
            out.append((sid, path))
    return out
