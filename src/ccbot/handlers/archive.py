"""Archive listing UI and lifecycle helpers.

Periodic sweeps:
  - idle_archive_sweep: when SESSION_IDLE_TTL is exceeded, archive a session.
  - purge_sweep: drop state.json records older than ARCHIVE_PURGE_AFTER.
    Transcripts on disk are kept for audit.

Interactive UI:
  - build_archive_page: render an archived-sessions page with inline buttons.
  - inspect, restore, delete callback handlers.
"""

from __future__ import annotations

import logging
import time

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..config import config
from ..session import Session, session_manager
from ..tmux_manager import tmux_manager
from .callback_data import (
    CB_ARC_ALL,
    CB_ARC_DELETE,
    CB_ARC_INSPECT,
    CB_ARC_PAGE,
    CB_ARC_RESTORE,
)
from .cleanup import clear_session_state

logger = logging.getLogger(__name__)

# How many archived sessions to render per /archive page.
PAGE_SIZE = 5

# Default lookback window for /archive (0-72h). /archive --all extends this.
DEFAULT_LOOKBACK_SECONDS = 72 * 3600


def _format_age(ts: float, now: float | None = None) -> str:
    if not ts:
        return "?"
    if now is None:
        now = time.time()
    delta = max(0.0, now - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def build_archive_page(
    *,
    page: int,
    lookback_seconds: float | None,
    show_all: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render one page of /archive."""
    sessions = session_manager.list_archived(max_age_seconds=lookback_seconds)
    total = len(sessions)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = sessions[start : start + PAGE_SIZE]

    header = "*Archived sessions*"
    if show_all:
        header += " (0–14d)"
    else:
        header += " (0–72h)"
    if total == 0:
        body = ["No archived sessions in this window."]
    else:
        body = [f"page {page + 1}/{pages} — {total} total"]
        for sess in chunk:
            label = "✓" if sess.state == "completed" else "·"
            ts = sess.archived_at or sess.last_event_at
            age = _format_age(ts) if ts else "?"
            usage = (
                f"{sess.token_usage_total // 1000}k tok"
                if sess.token_usage_total
                else "0 tok"
            )
            body.append(
                f"{label} `{sess.id}` *{sess.name}* — {sess.state}, {age}, {usage}"
            )

    rows: list[list[InlineKeyboardButton]] = []
    for sess in chunk:
        rows.append(
            [
                InlineKeyboardButton(
                    f"⤴ Restore {sess.name or sess.id}",
                    callback_data=f"{CB_ARC_RESTORE}{sess.id}"[:64],
                ),
                InlineKeyboardButton(
                    "🔍 Inspect",
                    callback_data=f"{CB_ARC_INSPECT}{sess.id}"[:64],
                ),
                InlineKeyboardButton(
                    "🗑 Delete",
                    callback_data=f"{CB_ARC_DELETE}{sess.id}"[:64],
                ),
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton("◀", callback_data=f"{CB_ARC_PAGE}{page - 1}")
        )
    nav_row.append(
        InlineKeyboardButton(
            ("→ 14d" if not show_all else "→ 72h"), callback_data=CB_ARC_ALL
        )
    )
    if page < pages - 1:
        nav_row.append(
            InlineKeyboardButton("▶", callback_data=f"{CB_ARC_PAGE}{page + 1}")
        )
    rows.append(nav_row)

    text = "\n".join([header, ""] + body)
    return text, InlineKeyboardMarkup(rows)


async def restore_session(bot: Bot, user_id: int, sess: Session) -> tuple[bool, str]:
    """Restore an archived session: create tmux window with `claude --resume`,
    re-attach the Session record, mark active.

    Returns (success, message).
    """
    del bot  # unused for now; reserved for future logging
    if sess.state in ("active", "idle"):
        return False, "Session already live"
    workdir = sess.workdir or ""
    if not workdir:
        return False, "No workdir on session record — cannot restore"

    success, message, created_wname, created_wid = await tmux_manager.create_window(
        workdir,
        resume_session_id=sess.claude_session_id or None,
    )
    if not success:
        return False, message

    hook_ok = await session_manager.wait_for_session_map_entry(
        created_wid, timeout=15.0
    )
    del hook_ok

    # If we did a --resume, override window_state to original sid (Claude allocates a new sid for the resume).
    if sess.claude_session_id:
        ws = session_manager.get_window_state(created_wid)
        if ws.session_id != sess.claude_session_id:
            ws.session_id = sess.claude_session_id
            ws.cwd = workdir
            ws.window_name = created_wname
            session_manager._save_state()

    session_manager.set_session_window(sess.id, created_wid)
    session_manager.set_active_session(user_id, sess.id)
    return True, f"Restored {sess.name or sess.id} ({created_wname})"


async def idle_archive_sweep(bot: Bot, user_id: int) -> int:
    """Archive any of the user's sessions that exceeded SESSION_IDLE_TTL.

    Returns number of sessions archived.
    """
    if config.session_idle_ttl <= 0:
        return 0
    candidates = session_manager.find_idle_to_archive(config.session_idle_ttl)
    archived = 0
    for sess in candidates:
        wid = sess.window_id
        if wid:
            w = await tmux_manager.find_window_by_id(wid)
            if w:
                await tmux_manager.kill_window(w.window_id)
            await clear_session_state(user_id, wid, bot)
        session_manager.mark_session_archived(sess.id, completed=False)
        archived += 1
    if archived:
        logger.info("Archived %d idle sessions", archived)
    return archived


def purge_sweep() -> int:
    """Drop state.json records past ARCHIVE_PURGE_AFTER. Returns number purged."""
    if config.archive_purge_after <= 0:
        return 0
    candidates = session_manager.find_archive_to_purge(config.archive_purge_after)
    purged = 0
    for sess in candidates:
        if session_manager.delete_session(sess.id):
            purged += 1
    if purged:
        logger.info(
            "Purged %d archive records older than %.0fs",
            purged,
            config.archive_purge_after,
        )
    return purged
