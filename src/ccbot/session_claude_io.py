"""Read-only Claude transcript discovery — encode cwd, list/get/locate sessions.

Pulled out of ``session.py`` so the SessionManager stays focused on the
DM-side state machine. None of these helpers mutate ``SessionManager``
state directly — they only return ``ClaudeSession`` records that the
caller routes into the manager's window/session bookkeeping.

Public API:
  encode_cwd(cwd) -> str — Claude's project directory naming convention
  build_session_file_path(session_id, cwd) -> Path | None
  get_session_direct(session_id, cwd) -> ClaudeSession | None
  list_sessions_for_directory(cwd) -> list[ClaudeSession]
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from .config import config
from .session_models import ClaudeSession
from .transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)


def encode_cwd(cwd: str) -> str:
    """Encode a cwd path to match Claude Code's project-directory naming.

    Replaces all non-alphanumeric characters (except dash) with dashes.
    E.g. ``/home/user_name/Code/project`` → ``-home-user-name-Code-project``.
    """
    return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)


def build_session_file_path(session_id: str, cwd: str) -> Path | None:
    """Direct path to ``<projects>/<encoded_cwd>/<session_id>.jsonl``."""
    if not session_id or not cwd:
        return None
    return config.claude_projects_path / encode_cwd(cwd) / f"{session_id}.jsonl"


def _parse_session_file(file_path: Path, session_id: str) -> ClaudeSession | None:
    """Synchronously walk a JSONL once → summary, last user msg, tokens, count.

    Kept sync (called via ``asyncio.to_thread``) because a single bulk
    ``read_text`` + per-line ``json.loads`` is several times faster than
    ``aiofiles``' ``async for line`` executor round-trips on large
    transcripts (multi-MB session files dominate the dir-picker path).
    """
    summary = ""
    last_user_msg = ""
    message_count = 0
    token_total = 0
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        message_count += 1
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "summary":
            s = data.get("summary", "")
            if s:
                summary = s
        elif data.get("type") == "assistant":
            usage = (data.get("message") or {}).get("usage") or {}
            token_total += int(usage.get("input_tokens", 0) or 0)
            token_total += int(usage.get("output_tokens", 0) or 0)
        elif TranscriptParser.is_user_message(data):
            parsed = TranscriptParser.parse_message(data)
            if parsed and parsed.text.strip():
                last_user_msg = parsed.text.strip()

    if not summary:
        summary = last_user_msg[:50] if last_user_msg else "Untitled"

    return ClaudeSession(
        session_id=session_id,
        summary=summary,
        message_count=message_count,
        file_path=str(file_path),
        token_total=token_total,
    )


async def get_session_direct(session_id: str, cwd: str) -> ClaudeSession | None:
    """Load a ``ClaudeSession`` from session_id + cwd, with glob fallback.

    Walks the JSONL once (in a worker thread) to extract the latest
    summary, last user message, cumulative token usage, and message count.
    """
    file_path = build_session_file_path(session_id, cwd)
    if not file_path or not file_path.exists():
        pattern = f"*/{session_id}.jsonl"
        matches = list(config.claude_projects_path.glob(pattern))
        if matches:
            file_path = matches[0]
            logger.debug("Found session via glob: %s", file_path)
        else:
            return None

    return await asyncio.to_thread(_parse_session_file, file_path, session_id)


async def list_sessions_for_directory(cwd: str) -> list[ClaudeSession]:
    """List existing Claude sessions for a directory.

    Encodes the cwd path to find the project directory under
    ``~/.claude/projects/<encoded_cwd>/``, globs ``*.jsonl`` files,
    and extracts summary info. Returns up to 10 sessions sorted by
    mtime (most recent first), skipping ``sessions-index``.
    """
    project_dir = config.claude_projects_path / encode_cwd(cwd)
    if not project_dir.is_dir():
        return []

    jsonl_files = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    # Read a bounded window of the newest transcripts concurrently rather
    # than awaiting each in turn — turns 10×read into ~max(read). The few
    # extra candidates beyond the 10-session cap absorb empty/aborted
    # files (message_count == 0) without a second round-trip.
    candidates = [f for f in jsonl_files if f.stem != "sessions-index"][:12]
    parsed = await asyncio.gather(
        *(get_session_direct(f.stem, cwd) for f in candidates)
    )
    sessions = [s for s in parsed if s and s.message_count > 0][:10]
    return sessions
