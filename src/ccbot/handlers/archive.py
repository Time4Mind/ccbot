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

# Per-session blurb cache — keyed by claude_session_id. The blurb is
# the first 1-3 user messages of the session, concatenated until the
# soft length budget kicks in. Archived JSONLs are append-frozen so a
# single scan covers the session's lifetime in archive.
_BLURB_CACHE: dict[str, str] = {}
# Soft cumulative budget across the blurb's combined user messages.
# We greedily include whole messages until adding the next one would
# push past the budget; the first message is always included whole
# even if it's longer (the user's own words trump the budget).
_BLURB_TOTAL_BUDGET = 240
# Hard cap on how many user messages can land in one blurb. Keeps the
# row short for chatty intros ("hi" / "go" / "do it") that wouldn't hit
# the byte budget on their own.
_BLURB_MAX_MESSAGES = 3

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


def _clean_user_msg(text: str) -> str:
    """Collapse whitespace and strip a leading slash-command prefix.

    Doesn't truncate — the budget is handled at the accumulation level
    in ``_collect_user_messages``. The leading-slash strip means a row
    that starts with ``/resume real ask`` reads ``real ask`` (the
    user's actual ask, not the dispatch verb).
    """
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if cleaned.startswith("/"):
        head, _, rest = cleaned.partition(" ")
        cleaned = rest if rest else head
    return cleaned.strip("` ")


async def _collect_user_messages(sess: Session) -> str:
    """Walk the JSONL and assemble a blurb from the first 1-3 real user
    messages.

    Stops accumulating as soon as the next message would push the
    combined length past ``_BLURB_TOTAL_BUDGET`` — but the *first*
    message is always included whole, even if it alone exceeds the
    budget (the user's own words trump the soft cap). Messages are
    joined with ``  \\n`` so each one renders on its own line in the
    rich-message body.

    Skips Claude Code's wrapper user-messages (``<system-reminder>``,
    ``<local-command-caveat>``, bash chrome) — these are CLI plumbing,
    not actual user prompts.
    """
    sid = sess.claude_session_id
    if not sid or not sess.workdir:
        return ""
    fp = build_session_file_path(sid, sess.workdir)
    if fp is None or not fp.exists():
        pattern = f"*/{sid}.jsonl"
        matches = list(config.claude_projects_path.glob(pattern))
        if not matches:
            return ""
        fp = matches[0]

    messages: list[str] = []
    total = 0
    scanned = 0
    try:
        async with aiofiles.open(fp, "r", encoding="utf-8") as f:
            async for line in f:
                scanned += 1
                # Honest sessions wedge the first 3 user messages
                # well inside the first 200 lines (system prelude +
                # opening assistant turn + 3 turns of dialogue). Cap
                # the scan to bail on long-running sessions.
                if scanned > 200:
                    break
                if len(messages) >= _BLURB_MAX_MESSAGES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not TranscriptParser.is_user_message(data):
                    continue
                parsed = TranscriptParser.parse_message(data)
                if not parsed or not parsed.text.strip():
                    continue
                raw = parsed.text.strip()
                if _RE_INJECTED_USER_MSG.search(raw):
                    continue
                cleaned = _clean_user_msg(raw)
                if not cleaned:
                    continue
                # First message: always include whole. Subsequent ones:
                # only if they still fit under the cumulative budget.
                if messages and total + len(cleaned) > _BLURB_TOTAL_BUDGET:
                    break
                messages.append(cleaned)
                total += len(cleaned)
    except OSError as e:
        logger.debug("archive blurb read failed for %s: %s", fp, e)
        return ""

    return "  \n".join(messages)


async def _archive_blurb(sess: Session) -> str:
    """Return the "what was this session about" line for an archived row.

    Source: the user's own first 1-3 messages from the JSONL transcript,
    concatenated until the soft length budget bites. No Haiku, no
    ai-title pickup, no spoiler hide — just the user's verbatim words
    in their own language. Cached forever per claude-session-id.
    """
    sid = sess.claude_session_id
    if not sid:
        return ""
    cached = _BLURB_CACHE.get(sid)
    if cached is not None:
        return cached
    blurb = await _collect_user_messages(sess)
    _BLURB_CACHE[sid] = blurb
    return blurb


def _display_name(sess: Session) -> str:
    """Human-readable form of ``sess.name`` — Haiku produces kebab-case
    (``archive-pagination-fix``); for the body row and the inline
    button label we render it with spaces (``archive pagination fix``)
    so it reads as a natural phrase. Directory-derived names
    (``workdir-2``) pass through the same transform without harm.
    """
    return (sess.name or sess.id).replace("-", " ")


logger = logging.getLogger(__name__)

# How many archived sessions to render per /archive page.
PAGE_SIZE = 6

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
    # cache (archived JSONLs don't change); cold paths walk the JSONL
    # once and pick up the first 1-3 user messages. Fan-out kept tiny
    # by PAGE_SIZE=6.
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
            display_name = _display_name(sess)
            # ``(lost)`` tag for sessions that hit the lost state before
            # archival (tmux window vanished externally; user never
            # ran Restore). Without it the row reads identical to a
            # clean archive and the recovery-skipped fact is invisible.
            lost_tag = " _(lost)_" if sess.was_lost else ""
            # Bold-wrap the index AND join sub-lines with ``  \n`` (hard
            # line break in CommonMark — two trailing spaces before the
            # newline). Without the hard break the rich parser treats
            # each row's blurb / workdir / goal as a soft break and
            # collapses the whole page into one wall-of-text paragraph.
            parts: list[str] = [
                f"**{idx}.** {label} *{display_name}*{lost_tag} — {age}"
            ]
            blurb = blurbs.get(sess.id) or ""
            if blurb:
                parts.append(blurb)
            wd = _shorten_workdir(sess.workdir) if sess.workdir else ""
            if wd:
                parts.append(f"`{wd}`")
            if sess.goal:
                parts.append(f"_{sess.goal}_")
            body.append("  \n".join(parts))

    # One button per session, paired up two-per-row (PAGE_SIZE=6 gives
    # 2+2+2). Each button's label carries the matching number so the
    # body line and button line up visually.
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, sess in enumerate(chunk, start=start + 1):
        row.append(
            InlineKeyboardButton(
                f"{idx}. {_display_name(sess)}",
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

    # Paragraph break (blank line) between the header, the page counter
    # and every session row — so the rich parser renders them as
    # separate blocks instead of one run-on paragraph.
    text = "\n\n".join([header, *body])
    return text, InlineKeyboardMarkup(rows)


async def restore_session(bot: Bot, user_id: int, sess: Session) -> tuple[bool, str]:
    """Restore an archived session: create tmux window with `claude --resume`,
    re-attach the Session record, mark active.

    Returns (success, message).
    """
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

    # A near-limit transcript auto-compacts on resume (60-110s) before it
    # accepts input. Flag the window so any prompts that arrive while
    # we're still compacting buffer into _pending_sends instead of being
    # typed mid-compaction. The background watcher drains the buffer
    # once the pane settles AND keeps Telegram TYPING refreshed so the
    # chat doesn't look frozen during the wait.
    if sess.claude_session_id:
        session_manager.mark_window_resuming(created_wid, bot=bot, user_id=user_id)

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
    note = ""
    if sess.claude_session_id:
        note = " — if it was a large session it may compact for a minute; your first message is held until it's ready."
    return True, f"Restored {sess.name or sess.id} ({created_wname}){note}"


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
