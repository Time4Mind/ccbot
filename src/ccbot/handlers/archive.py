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

# Per-session blurb cache — keyed by claude_session_id. The cached
# tuple holds *both* candidate sources: Claude Code's own
# ``"type":"ai-title"`` summary (always English) and the session's first
# real user message (whatever language the user typed). At render time
# we pick by the user's UI language: ``en`` → ai-title, anything else →
# first user message. Caching both lets the user flip languages without
# rescanning the JSONL. Archived JSONLs are append-frozen so a single
# scan is enough for the session's lifetime in archive.
_BLURB_CACHE: dict[str, tuple[str, str]] = {}
# Visible head before the spoiler kicks in. ~12-15 English / ~8-10
# Cyrillic words land inside this budget; the rest of the message goes
# into an inline ``||spoiler||`` tail (tap to reveal) instead of the
# old ellipsis truncation.
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
    """Compact a blurb candidate into one short line.

    Strips markdown noise (backticks, leading bullets), collapses
    whitespace, and — when the cleaned text exceeds ``_BLURB_MAX_LEN``
    — hides the overflow inside a Telegram inline spoiler
    (``||tail||``) so the reader can tap to reveal the rest. Pure
    truncation (the old ``…`` ellipsis) silently lost content.

    Existing ``||`` runs in the source are escaped so we can't open or
    close the spoiler in the wrong place.
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
    # Neutralise any naturally-occurring ``||`` runs in the source —
    # they'd otherwise open / close our outer spoiler in the wrong
    # place. A single space between the pipes kills the delimiter
    # without dropping content. Telegram's rich parser doesn't honour
    # backslash escapes the way CommonMark does (verified live on PR
    # #112 — a stray ``\.`` leaked the backslash visibly), so we don't
    # rely on ``\|\|`` either.
    cleaned = cleaned.replace("||", "| |")
    if len(cleaned) <= _BLURB_MAX_LEN:
        return cleaned
    # Prefer a word boundary close to the budget so we don't split
    # mid-word ("Посмотри что за дан||ные здесь есть…").
    cut = cleaned.rfind(" ", 0, _BLURB_MAX_LEN)
    if cut < _BLURB_MAX_LEN - 24:
        cut = _BLURB_MAX_LEN
    head = cleaned[:cut].rstrip()
    tail = cleaned[cut:].lstrip()
    if not tail:
        return head
    return f"{head}||{tail}||"


async def _scan_blurb_sources(sess: Session) -> tuple[str, str]:
    """Walk the JSONL once and pull both blurb candidates.

    Returns ``(ai_title, first_user_msg)`` — either field can be empty.
    The ``"ai-title"`` row Claude Code itself emits is always English and
    drives the en-locale path; the first real user message is the
    fallback (and the preferred source for ru/zh users when Haiku
    translation is unavailable).
    """
    sid = sess.claude_session_id
    if not sid or not sess.workdir:
        return "", ""
    fp = build_session_file_path(sid, sess.workdir)
    if fp is None or not fp.exists():
        pattern = f"*/{sid}.jsonl"
        matches = list(config.claude_projects_path.glob(pattern))
        if not matches:
            return "", ""
        fp = matches[0]

    ai_title = ""
    first_user_msg = ""
    scanned = 0
    try:
        async with aiofiles.open(fp, "r", encoding="utf-8") as f:
            async for line in f:
                scanned += 1
                # ai-title typically lands around line 5-10 (first
                # assistant turn). 60-line cap leaves headroom for a
                # bigger system-prompt prelude before bailing out.
                if scanned > 60:
                    break
                if ai_title and first_user_msg:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not ai_title and data.get("type") == "ai-title":
                    candidate = (data.get("aiTitle") or "").strip()
                    if candidate:
                        ai_title = candidate
                        continue
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
        return "", ""

    return ai_title, first_user_msg


async def _archive_blurb(sess: Session, user_id: int) -> str:
    """Return a localised "what was this session about" line.

    Source picked by the user's UI language:

    * ``en`` — the ``ai-title`` row Claude Code itself emits (also
      English, ~5-8 words, on-topic).
    * Anything else — same ``ai-title`` translated by a one-shot Haiku
      call when ``haiku_naming`` is on, falling back to the first user
      message verbatim. Translations are persisted in ``state.json``
      under ``archive_blurb_translations`` so a bot restart never pays
      for the same Haiku call twice.

    JSONL scan and translation are guarded by per-source caches.
    """
    sid = sess.claude_session_id
    if not sid:
        return ""
    cached = _BLURB_CACHE.get(sid)
    if cached is None:
        cached = await _scan_blurb_sources(sess)
        _BLURB_CACHE[sid] = cached
    ai_title, first_user_msg = cached

    settings = session_manager.get_user_settings(user_id)
    lang = str(settings.get("language", "en") or "en")
    haiku_on = bool(settings.get("haiku_naming", True))

    if lang == "en":
        return _trim_blurb(ai_title or first_user_msg)

    if ai_title and haiku_on:
        translated = session_manager.get_archive_blurb_translation(sid, lang)
        if translated is None:
            from ..naming import translate_to

            try:
                translated = await translate_to(ai_title, lang)
            except Exception as e:
                logger.debug("ai-title translate failed for %s: %s", sid, e)
                translated = None
            if translated:
                session_manager.set_archive_blurb_translation(sid, lang, translated)
        if translated:
            return _trim_blurb(translated)

    # Fallback: the user's own words. Already in their language.
    return _trim_blurb(first_user_msg or ai_title)


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
    # cache (archived JSONLs don't change) or the persisted translation
    # cache; cold paths walk the JSONL once and may fire one Haiku
    # translate call. Fan-out kept tiny by PAGE_SIZE=6.
    blurbs: dict[str, str] = {}
    for sess in chunk:
        try:
            blurbs[sess.id] = await _archive_blurb(sess, user_id)
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
