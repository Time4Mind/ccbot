"""Archived transcript rendering helpers for history and quick inspection.

Caches and path resolution are supplied by handlers.history so its mutable
state identity and monkeypatch-visible path seam remain unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles

from ..session import Session
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser
from ..transcript_types import ParsedEntry
from .card_seed_io import load_recent_parsed_entries_with_older

if TYPE_CHECKING:
    from .card_model import CardState


def _archived_card_state(parsed_list: list[ParsedEntry]) -> CardState:
    """Convert parsed transcript rows into a completed card event stream."""
    from ..session_monitor import NewMessage
    from .card_model import CardState, _apply_tool_result, _build_event

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

    # Archived events have finished. Otherwise the last event on a page
    # acquires a spurious live elapsed-time indicator.
    for ev in state.events:
        if ev.completed_at is None:
            ev.completed_at = ev.started_at
    return state


def _archived_card_path(
    sess: Session, session_file_path: Callable[[Session], Path | None], config: Any
) -> Path | None:
    sid = sess.claude_session_id
    if not sid or not sess.workdir:
        return None
    fp = session_file_path(sess)
    if fp is not None and fp.exists():
        return fp
    if sess.backend == "codex":
        return None
    matches = list(config.claude_projects_path.glob(f"*/{sid}.jsonl"))
    return matches[0] if matches else None


def _render_archived_card_preview_sync(
    sess: Session,
    user_id: int | None,
    *,
    session_file_path: Callable[[Session], Path | None],
    config: Any,
    logger: logging.Logger,
) -> tuple[str, bool] | None:
    fp = _archived_card_path(sess, session_file_path, config)
    if fp is None:
        return None
    try:
        parsed_list, has_older = load_recent_parsed_entries_with_older(fp, 1)
    except (OSError, ValueError) as exc:
        logger.debug("archived card tail read failed for %s: %s", fp, exc)
        return None
    if not parsed_list:
        return None

    from .card_model import paginate_events_for_card, render_page

    state = _archived_card_state(parsed_list)
    if not state.events:
        return None
    pages = paginate_events_for_card(state, user_id)
    body = render_page(pages[-1], time.time())
    header = f"📦 [{sess.name or sess.id}]"
    text = f"{header}\n\n{body}" if body.strip() else header
    return text, has_older or len(pages) > 1


async def render_archived_card_preview_impl(
    sess: Session,
    user_id: int | None,
    *,
    session_file_path: Callable[[Session], Path | None],
    config: Any,
    logger: logging.Logger,
) -> tuple[str, bool] | None:
    """Render only the last card page without parsing the full archive."""
    return await asyncio.to_thread(
        _render_archived_card_preview_sync,
        sess,
        user_id,
        session_file_path=session_file_path,
        config=config,
        logger=logger,
    )


async def render_archived_card_pages_impl(
    sess: Session,
    user_id: int | None,
    *,
    session_file_path: Callable[[Session], Path | None],
    archived_card_cache: dict[str, tuple[float, int, list[str], int]],
    config: Any,
    logger: logging.Logger,
) -> tuple[list[str], int] | None:
    """Render all of an archived session's transcript with the live-card engine.

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
    fp = _archived_card_path(sess, session_file_path, config)
    if fp is None:
        return None

    try:
        st = fp.stat()
    except OSError:
        return None

    cached = archived_card_cache.get(sid)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return list(cached[2]), cached[3]

    # Lazy imports — card_model pulls in the whole notification model layer;
    # keep it off history.py's import-time path (and avoid any cycle).
    from .card_model import paginate_events_for_card, render_page

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

    state = _archived_card_state(parsed_list)
    if not state.events:
        return None

    now = time.time()
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

    Used for full archived history without requiring a live tmux window.
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
