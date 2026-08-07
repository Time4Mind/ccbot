"""Archived transcript rendering helpers for history.

Caches and path resolution are supplied by handlers.history so its mutable
state identity and monkeypatch-visible path seam remain unchanged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiofiles

from ..session import Session
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser


async def render_archived_card_pages_impl(
    sess: Session,
    user_id: int | None,
    *,
    session_file_path: Callable[[Session], Path | None],
    archived_card_cache: dict[str, tuple[float, int, list[str], int]],
    config: Any,
    logger: logging.Logger,
) -> tuple[list[str], int] | None:
    """Render an archived session's transcript with the live-card engine.

    Unlike :func:`render_archived_history_pages` — which flattens every
    message into one page and strips the expandable-quote sentinels, so
    long thinking / tool outputs dump inline as an unreadable wall — this
    reuses ``card_model``'s event pipeline (``_build_event`` +
    ``_apply_tool_result`` + ``paginate_events_for_card`` + ``render_page``).
    Thinking blocks and tool bodies collapse into ``<details>`` spoilers
    exactly like the active session card, and pagination follows answer
    boundaries + the user's line budget.

    Returns ``(pages, event_count)`` or ``None`` when no transcript
    resolves (no claude_session_id, missing file, empty transcript).
    """
    sid = sess.claude_session_id
    if not sid or not sess.workdir:
        return None
    fp = session_file_path(sess)
    if fp is None or not fp.exists():
        if sess.backend == "codex":
            return None
        # Glob fallback — cwd on the record may have shifted since archival.
        pattern = f"*/{sid}.jsonl"
        matches = list(config.claude_projects_path.glob(pattern))
        if not matches:
            return None
        fp = matches[0]

    try:
        st = fp.stat()
    except OSError:
        return None

    cached = archived_card_cache.get(sid)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return list(cached[2]), cached[3]

    # Lazy imports — card_model pulls in the whole notification model layer;
    # keep it off history.py's import-time path (and avoid any cycle).
    import time as _time

    from ..session_monitor import NewMessage
    from .card_model import (
        CardState,
        _apply_tool_result,
        _build_event,
        paginate_events_for_card,
        render_page,
    )

    try:
        raw = fp.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("archived card read failed for %s: %s", fp, e)
        return None
    raw_entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw_entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    try:
        parsed_list, _ = TranscriptParser.parse_entries(raw_entries, pending_tools=None)
    except Exception as e:
        logger.debug("archived card parse failed for %s: %s", fp, e)
        return None
    if not parsed_list:
        return None

    # ParsedEntry → NewMessage → Event, folding tool_results into their
    # matching tool_use (same loop the live-card JSONL seed uses).
    state = CardState()
    for p in parsed_list:
        ct = getattr(p, "content_type", "text")
        msg = NewMessage(
            session_id="archive",
            text=getattr(p, "text", "") or "",
            is_complete=True,
            content_type=ct,
            tool_use_id=getattr(p, "tool_use_id", None),
            role=getattr(p, "role", "assistant"),
            tool_name=getattr(p, "tool_name", None),
            image_data=getattr(p, "image_data", None),
            stop_reason=getattr(p, "stop_reason", None),
            timestamp=getattr(p, "timestamp", "") or "",
            is_error=getattr(p, "is_error", False),
        )
        ev = _build_event(msg)
        if ct == "tool_result" and _apply_tool_result(state, ev):
            continue
        state.events.append(ev)
    if not state.events:
        return None

    # Every event in an archived transcript is finished — nothing is
    # streaming. Stamp ``completed_at`` so ``_is_in_flight`` never flags
    # the terminal event of a page as live and renders a bogus ``⏳
    # 3968:24`` elapsed against ``now`` instead of the entry's HH:MM.
    for ev in state.events:
        if ev.completed_at is None:
            ev.completed_at = ev.started_at

    now = _time.time()
    label = sess.name or sess.id
    header = f"📦 [{label}]"
    pages_events = paginate_events_for_card(state, user_id)
    pages: list[str] = []
    for pe in pages_events:
        body = render_page(pe, now)
        pages.append(f"{header}\n\n{body}" if body.strip() else header)
    total = len(state.events)
    archived_card_cache[sid] = (st.st_mtime, st.st_size, list(pages), total)
    return list(pages), total


async def render_archived_history_pages_impl(
    sess: Session,
    *,
    session_file_path: Callable[[Session], Path | None],
    archived_pages_cache: dict[str, tuple[float, int, list[str], int]],
    config: Any,
    logger: logging.Logger,
) -> tuple[list[str], int] | None:
    """Read ``sess``'s on-disk JSONL transcript and return Telegram-ready
    pages + total message count. Returns ``None`` when there's no
    resolvable transcript (no claude_session_id, missing file, etc.).

    Used by the Archive → Inspect view to surface what the session
    actually did, without requiring a live tmux window.
    """
    sid = sess.claude_session_id
    if not sid or not sess.workdir:
        return None
    fp = session_file_path(sess)
    if fp is None or not fp.exists():
        if sess.backend == "codex":
            return None
        # Glob fallback — the cwd column on the Session may have shifted
        # since archival (rare, but cheap to handle).
        pattern = f"*/{sid}.jsonl"
        matches = list(config.claude_projects_path.glob(pattern))
        if not matches:
            return None
        fp = matches[0]

    try:
        st = fp.stat()
    except OSError:
        return None

    cached = archived_pages_cache.get(sid)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return list(cached[2]), cached[3]

    entries: list[dict[str, Any]] = []
    try:
        async with aiofiles.open(fp, "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = TranscriptParser.parse_line(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if data:
                    entries.append(data)
    except OSError as e:
        logger.debug("archived history read failed for %s: %s", fp, e)
        return None

    parsed_entries, _ = TranscriptParser.parse_entries(entries)
    messages = [
        {
            "role": e.role,
            "text": e.text,
            "content_type": e.content_type,
            "timestamp": e.timestamp,
        }
        for e in parsed_entries
    ]
    if not config.show_user_messages:
        messages = [m for m in messages if m["role"] == "assistant"]
    # Drop tool_use rows — same rationale as ``prewarm_pages_cache``:
    # the parser emits both tool_use (header only) and tool_result
    # (header + body) for each call, so the bare tool_use rows are pure
    # duplicates in the rendered view.
    messages = [m for m in messages if m.get("content_type") != "tool_use"]
    total = len(messages)
    if total == 0:
        return None

    _qstart = TranscriptParser.EXPANDABLE_QUOTE_START
    _qend = TranscriptParser.EXPANDABLE_QUOTE_END
    label = sess.name or sess.id
    lines: list[str] = [f"📦 [{label}] Archived transcript ({total} msgs)"]
    for msg in messages:
        ts = msg.get("timestamp")
        hh_mm = ""
        if ts:
            try:
                time_part = ts.split("T")[1] if "T" in ts else ts
                hh_mm = time_part[:5]
            except (IndexError, TypeError):
                hh_mm = ""
        lines.append(f"───── {hh_mm} ─────" if hh_mm else "─────────────")
        msg_text = (msg.get("text") or "").replace(_qstart, "").replace(_qend, "")
        fence_lines = sum(
            1 for ln in msg_text.split("\n") if ln.strip().startswith("```")
        )
        if fence_lines % 2 == 1:
            msg_text = msg_text + "\n```"
        role = msg.get("role", "assistant")
        ctype = msg.get("content_type", "text")
        if role == "user":
            lines.append(f"👤 {msg_text}")
        elif ctype == "thinking":
            lines.append(f"∴ Thinking…\n{msg_text}")
        else:
            lines.append(msg_text)

    full = "\n\n".join(lines)
    pages = split_message(full, max_length=4096)
    archived_pages_cache[sid] = (st.st_mtime, st.st_size, list(pages), total)
    return list(pages), total
