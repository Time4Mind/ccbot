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

from pathlib import Path

from ..config import config
from ..i18n import t
from ..session import Session, session_manager
from ..tmux_manager import tmux_manager
from .callback_data import (
    CB_ARC_ALL,
    CB_ARC_INSPECT,
    CB_ARC_PAGE,
)
from .cleanup import clear_session_state


def _shorten_workdir(path: str) -> str:
    """Replace the user's home prefix with ``~`` so paths fit on one row.
    Mirrors ``bot._common.shorten_workdir`` — kept here to avoid a
    handlers→bot import inversion."""
    if not path:
        return ""
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


logger = logging.getLogger(__name__)

# How many archived sessions to render per /archive page.
PAGE_SIZE = 5

# Default lookback window for /archive (0-72h). /archive --all extends this.
DEFAULT_LOOKBACK_SECONDS = 72 * 3600


def _format_age(user_id: int, ts: float, now: float | None = None) -> str:
    """Compact human age (``5m``, ``3h``, ``2d``) with localized ``ago`` suffix."""
    if not ts:
        return "?"
    if now is None:
        now = time.time()
    delta = max(0.0, now - ts)
    if delta < 60:
        return t(user_id, "archive.age.s", n=int(delta))
    if delta < 3600:
        return t(user_id, "archive.age.m", n=int(delta / 60))
    if delta < 86400:
        return t(user_id, "archive.age.h", n=int(delta / 3600))
    return t(user_id, "archive.age.d", n=int(delta / 86400))


def build_archive_page(
    *,
    page: int,
    lookback_seconds: float | None,
    show_all: bool,
    user_id: int,
    back_callback: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render one page of /archive.

    ``back_callback`` — when set, append a final Back row pointing at that
    callback. Used by the Menu→Archive entry path (and its in-archive
    navigation) so the user can always escape back to Menu without
    relying on Telegram's reply-thread depth. The ``/archive`` slash
    command passes ``None`` because its message is a fresh reply, not a
    Menu-rooted view.
    """
    sessions = session_manager.list_archived(max_age_seconds=lookback_seconds)
    total = len(sessions)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = sessions[start : start + PAGE_SIZE]

    title = t(user_id, "archive.title")
    range_suffix = t(user_id, "archive.range_14d" if show_all else "archive.range_72h")
    header = f"*{title}*{range_suffix}"
    if total == 0:
        body = [t(user_id, "archive.empty")]
    else:
        body = [
            t(user_id, "archive.page_line", page=page + 1, pages=pages, total=total)
        ]
        for sess in chunk:
            # ✓ marks /done-completed sessions; · is plain archive.
            # We drop the explicit "archived" / "completed" word from
            # the body — it's tautological inside the Archive view, and
            # the prefix glyph already carries the distinction.
            label = "✓" if sess.state == "completed" else "·"
            ts = sess.archived_at or sess.last_event_at
            age = _format_age(user_id, ts) if ts else "?"
            usage = (
                t(user_id, "archive.tokens_k", k=sess.token_usage_total // 1000)
                if sess.token_usage_total
                else t(user_id, "archive.tokens_zero")
            )
            display_name = sess.name or sess.id
            line = f"{label} *{display_name}* — {age} · {usage}"
            wd = _shorten_workdir(sess.workdir) if sess.workdir else ""
            if wd:
                line += f"\n  `{wd}`"
            if sess.goal:
                line += f"\n  _{sess.goal}_"
            body.append(line)

    rows: list[list[InlineKeyboardButton]] = []
    for sess in chunk:
        # One button per session — opens the Inspect view, which renders
        # the transcript history and surfaces Restore / Delete inline.
        # Keeping just a single, full-width affordance per row keeps
        # the list scannable when there are many archived sessions.
        rows.append(
            [
                InlineKeyboardButton(
                    t(user_id, "btn.open_session", name=sess.name or sess.id),
                    callback_data=f"{CB_ARC_INSPECT}{sess.id}"[:64],
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
            t(user_id, "btn.to_14d" if not show_all else "btn.to_72h"),
            callback_data=CB_ARC_ALL,
        )
    )
    if page < pages - 1:
        nav_row.append(
            InlineKeyboardButton("▶", callback_data=f"{CB_ARC_PAGE}{page + 1}")
        )
    rows.append(nav_row)

    if back_callback is not None:
        rows.append(
            [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=back_callback)]
        )

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
            session_manager.save_state()

    session_manager.set_session_window(sess.id, created_wid)
    session_manager.set_active_session(user_id, sess.id)
    if session_manager.get_user_settings(user_id).get("local_terminal") == "auto":
        from ..local_terminal import open_terminal_for_window

        await open_terminal_for_window(created_wid, user_id=user_id)
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
