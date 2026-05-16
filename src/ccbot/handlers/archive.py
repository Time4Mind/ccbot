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

import json
import logging
import re
import time

import aiofiles
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from pathlib import Path

from ..config import config
from ..i18n import t
from ..session import Session, session_manager
from ..session_claude_io import build_session_file_path
from ..tmux_manager import tmux_manager
from ..transcript_parser import TranscriptParser
from .callback_data import (
    CB_ARC_ALL,
    CB_ARC_INSPECT,
    CB_ARC_PAGE,
)
from .cleanup import clear_session_state

# Per-session blurb cache — keyed by claude_session_id. The blurb is a
# short "what was this session about" snippet pulled from the JSONL
# (first ``type=summary`` row, else the first user message). Archived
# JSONLs don't grow, so a single lookup is enough for the session's
# lifetime in archive. The dict is bounded by the number of archived
# sessions on this host — fine to keep in memory.
_BLURB_CACHE: dict[str, str] = {}
# A "few words more" than a one-liner — leaves headroom for ~12-15
# English / ~8-10 Cyrillic words before the ellipsis.
_BLURB_MAX_LEN = 96

# Claude Code injects its own "user" messages — local-command caveats,
# system reminders, bash plumbing chrome — alongside the genuine user
# prompt. Skipping these when sniffing the first real message keeps the
# archive blurb on-topic. Pattern matches the opening tag (same list as
# ``TranscriptParser._RE_SYSTEM_TAGS``, kept local to avoid a private-
# attribute lint warning).
_RE_INJECTED_USER_MSG = re.compile(
    r"<(bash-input|bash-stdout|bash-stderr|local-command-caveat|system-reminder)"
)


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


def _trim_blurb(text: str) -> str:
    """Compact a JSONL summary or first user message into one short line.

    Strips markdown noise (backticks, leading bullets), collapses
    whitespace, and clamps to ``_BLURB_MAX_LEN`` chars with an ellipsis.
    """
    if not text:
        return ""
    # Collapse newlines + runs of whitespace.
    cleaned = " ".join(text.split())
    # Drop a leading "/cmd " prefix so blurbs of /restore / /resume calls
    # don't read as "/resume restart pls" — the user's actual ask is the
    # rest of the line.
    if cleaned.startswith("/"):
        head, _, rest = cleaned.partition(" ")
        cleaned = rest if rest else head
    cleaned = cleaned.strip("` ")
    if len(cleaned) <= _BLURB_MAX_LEN:
        return cleaned
    return cleaned[: _BLURB_MAX_LEN - 1].rstrip() + "…"


async def _archive_blurb(sess: Session) -> str:
    """Return a short "what was this session about" line for an archived
    session. Reads at most the first few JSONL entries — bails out as
    soon as it has a summary or a user message. Cached forever per
    ``claude_session_id`` (archived JSONLs are append-frozen).
    """
    sid = sess.claude_session_id
    if not sid or not sess.workdir:
        return ""
    cached = _BLURB_CACHE.get(sid)
    if cached is not None:
        return cached
    fp = build_session_file_path(sid, sess.workdir)
    if fp is None or not fp.exists():
        pattern = f"*/{sid}.jsonl"
        matches = list(config.claude_projects_path.glob(pattern))
        if not matches:
            _BLURB_CACHE[sid] = ""
            return ""
        fp = matches[0]

    summary = ""
    first_user_msg = ""
    scanned = 0
    try:
        async with aiofiles.open(fp, "r", encoding="utf-8") as f:
            async for line in f:
                scanned += 1
                # 40-line cap: a summary row, when present, is at the
                # top; the first user message is also near the top.
                # Bail out instead of walking the whole JSONL.
                if scanned > 40:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "summary":
                    s = data.get("summary", "")
                    if s:
                        summary = s
                        break
                if not first_user_msg and TranscriptParser.is_user_message(data):
                    parsed = TranscriptParser.parse_message(data)
                    if parsed and parsed.text.strip():
                        # Skip Claude Code's wrapper user-messages — the
                        # ``<local-command-caveat>``, ``<system-reminder>``,
                        # bash-stdout chrome etc. These are injected by the
                        # CLI itself, not actual user prompts, and would
                        # otherwise leak into the archive blurb as a wall of
                        # boilerplate.
                        candidate = parsed.text.strip()
                        if not _RE_INJECTED_USER_MSG.search(candidate):
                            first_user_msg = candidate
    except OSError as e:
        logger.debug("archive blurb read failed for %s: %s", fp, e)
        _BLURB_CACHE[sid] = ""
        return ""

    blurb = _trim_blurb(summary or first_user_msg)
    _BLURB_CACHE[sid] = blurb
    return blurb


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


async def build_archive_page(
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

    Async because we read each session's JSONL once to extract a short
    "what was this session about" blurb — cached by claude_session_id
    so subsequent paints are instant.
    """
    sessions = session_manager.list_archived(max_age_seconds=lookback_seconds)
    total = len(sessions)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = sessions[start : start + PAGE_SIZE]

    # Precompute blurbs for the chunk. Most calls hit the in-memory
    # cache (archived JSONLs don't change); cold paths read at most
    # ~40 lines per JSONL. Fan-out kept tiny by PAGE_SIZE=5.
    blurbs: dict[str, str] = {}
    for sess in chunk:
        try:
            blurbs[sess.id] = await _archive_blurb(sess)
        except Exception as e:
            logger.debug("archive blurb fetch failed for %s: %s", sess.id, e)
            blurbs[sess.id] = ""

    title = t(user_id, "archive.title")
    range_suffix = t(user_id, "archive.range_14d" if show_all else "archive.range_72h")
    header = f"*{title}*{range_suffix}"
    if total == 0:
        body = [t(user_id, "archive.empty")]
    else:
        body = [
            t(user_id, "archive.page_line", page=page + 1, pages=pages, total=total)
        ]
        for idx, sess in enumerate(chunk, start=start + 1):
            # ✓ marks /done-completed sessions; · is plain archive.
            label = "✓" if sess.state == "completed" else "·"
            ts = sess.archived_at or sess.last_event_at
            age = _format_age(user_id, ts) if ts else "?"
            display_name = sess.name or sess.id
            # ``(lost)`` tag for sessions that hit the lost state before
            # archival (tmux window vanished externally; user never
            # ran Restore). Without it the row reads identical to a
            # clean archive and the recovery-skipped fact is invisible.
            lost_tag = " _(lost)_" if sess.was_lost else ""
            line = f"{idx}. {label} *{display_name}*{lost_tag} — {age}"
            blurb = blurbs.get(sess.id) or ""
            if blurb:
                line += f"\n  {blurb}"
            wd = _shorten_workdir(sess.workdir) if sess.workdir else ""
            if wd:
                line += f"\n  `{wd}`"
            if sess.goal:
                line += f"\n  _{sess.goal}_"
            body.append(line)

    # One button per session, paired up two-per-row (PAGE_SIZE=5 gives
    # 2+2+1). Each button's label carries the matching number so the
    # body line and button line up visually.
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, sess in enumerate(chunk, start=start + 1):
        name = sess.name or sess.id
        row.append(
            InlineKeyboardButton(
                f"{idx}. {name}",
                callback_data=f"{CB_ARC_INSPECT}{sess.id}"[:64],
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

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
        if sess.claude_session_id:
            await tmux_manager.kill_orphan_claude_processes(sess.claude_session_id)
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
