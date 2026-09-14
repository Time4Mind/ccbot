"""Archive listing UI, restore lifecycle, and periodic cleanup sweeps.

Pure archive text normalization lives in ``archive_blurb``. Stateful helpers
remain here to preserve historical imports and monkeypatch seams around
``session_manager``, ``tmux_manager``, ``config`` and blurb collection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiofiles
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..config import config
from ..i18n import t
from ..rich import RICH_TABLE_NORMAL_FONT
from ..session import (
    DEFAULT_IDLE_ARCHIVE_HOURS,
    IDLE_ARCHIVE_HOUR_CHOICES,
    Session,
    session_manager,
)
from ..session_claude_io import build_session_file_path
from ..tmux_manager import tmux_manager
from ..transcript_parser import TranscriptParser
from .archive_blurb import (
    _RE_INJECTED_USER_MSG,
    _RE_SYSTEM_UI_TEXT,
    _clean_user_msg,
    _display_name,
    _shorten_workdir,
    _truncate_at_word,
)
from .callback_data import CB_ARC_INSPECT, CB_ARC_PAGE
from .cleanup import teardown_session_runtime

logger = logging.getLogger(__name__)

_BLURB_CACHE: dict[str, str] = {}
_BLURB_TOTAL_BUDGET = 240
_BLURB_MAX_MESSAGES = 3
PAGE_SIZE = 6
DEFAULT_LOOKBACK_SECONDS = 20 * 86400


def _format_blurb(messages: list[str]) -> str:
    """Lay out early prompts with an independent per-prompt display cap."""
    if not messages:
        return ""
    sep = "  \n"
    return sep.join(
        _truncate_at_word(message, _BLURB_TOTAL_BUDGET) for message in messages
    )


async def _collect_user_messages(sess: Session) -> str:
    """Walk the JSONL and assemble a blurb from the first 1-3 real user
    messages.

    Keeps scanning until the earliest three usable requests are found.
    Each request is independently truncated only for display, so a long
    first request can never make the second one disappear.

    Skips Claude Code's wrapper user-messages (``<system-reminder>``,
    ``<local-command-caveat>``, bash chrome) and its own UI events
    (``[Request interrupted by user]``, ``Set model to …``, etc.) —
    these are CLI plumbing, not actual user prompts.
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
    scanned = 0
    try:
        async with aiofiles.open(fp, "r", encoding="utf-8") as f:
            async for line in f:
                scanned += 1
                if len(messages) >= _BLURB_MAX_MESSAGES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if sess.backend == "codex":
                    payload = data.get("payload")
                    raw_messages: list[str] = []
                    if (
                        data.get("type") != "event_msg"
                        or not isinstance(payload, dict)
                        or payload.get("type") != "user_message"
                    ):
                        if (
                            data.get("type") != "response_item"
                            or not isinstance(payload, dict)
                            or payload.get("type") != "message"
                            or payload.get("role") != "user"
                        ):
                            continue
                        content = payload.get("content")
                        if not isinstance(content, list):
                            continue
                        raw_messages = [
                            str(item.get("text") or "").strip()
                            for item in content
                            if isinstance(item, dict)
                            and item.get("type") == "input_text"
                        ]
                    else:
                        raw_messages = [str(payload.get("message") or "").strip()]
                    if not any(raw_messages):
                        continue
                else:
                    if not TranscriptParser.is_user_message(data):
                        continue
                    parsed = TranscriptParser.parse_message(data)
                    if not parsed or not parsed.text.strip():
                        continue
                    raw_messages = [parsed.text.strip()]
                for raw in raw_messages:
                    if (
                        not raw
                        or _RE_INJECTED_USER_MSG.search(raw)
                        or _RE_SYSTEM_UI_TEXT.match(raw)
                        or raw.startswith("# AGENTS.md instructions")
                        or raw.startswith("<environment_context>")
                        or raw.startswith("<turn_aborted>")
                    ):
                        continue
                    cleaned = _clean_user_msg(raw)
                    if not cleaned or (messages and cleaned == messages[-1]):
                        continue
                    messages.append(cleaned)
                    if len(messages) >= _BLURB_MAX_MESSAGES:
                        break
    except OSError as e:
        logger.debug("archive blurb read failed for %s: %s", fp, e)
        return ""

    return _format_blurb(messages)


async def _archive_blurb(sess: Session, user_id: int | None = None) -> str:
    """Return the "what was this session about" line for an archived row.

    Source: the first two real user messages from the JSONL transcript. With
    ``archive_ai_description`` enabled, the cheap naming model condenses them;
    otherwise both prompts are shown with a middle-dot prefix. Results are
    cached because archived transcripts are append-frozen.
    """
    sid = sess.claude_session_id
    if not sid:
        return ""
    if user_id is None:
        cached = _BLURB_CACHE.get(sid)
        if cached is not None:
            return cached
        blurb = await _collect_user_messages(sess)
        _BLURB_CACHE[sid] = blurb
        return blurb
    ai_enabled = bool(
        session_manager.get_user_settings(user_id).get(
            "archive_ai_description", False
        )
    )
    cache_key = f"{sid}:{int(ai_enabled)}"
    cached = _BLURB_CACHE.get(cache_key)
    if cached is not None:
        return cached
    blurb = await _collect_user_messages(sess)
    messages = [part.strip() for part in blurb.split("  \n") if part.strip()][:2]
    if ai_enabled and messages:
        from ..naming import generate_description

        generated = await generate_description(messages, backend=sess.backend)
        if generated:
            _BLURB_CACHE[cache_key] = generated
            return generated
    fallback = "<br>".join(f"· {message}" for message in messages)
    _BLURB_CACHE[cache_key] = fallback
    return fallback


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
    del show_all
    lookback_seconds = DEFAULT_LOOKBACK_SECONDS
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
    async def _safe_blurb(sess: Session) -> tuple[str, str]:
        try:
            return sess.id, await _archive_blurb(sess, user_id)
        except Exception as e:
            logger.debug("archive blurb fetch failed for %s: %s", sess.id, e)
            return sess.id, ""

    blurbs = dict(await asyncio.gather(*(_safe_blurb(sess) for sess in chunk)))

    title = t(user_id, "archive.title")
    header = f"*{title}*"
    if total == 0:
        body = [t(user_id, "archive.empty")]
    else:
        body = []
        table_rows = [
            f"| {t(user_id, 'archive.column.session')} | "
            f"{t(user_id, 'archive.column.description')}{RICH_TABLE_NORMAL_FONT} |",
            "|---|---|",
        ]
        for idx, sess in enumerate(chunk, start=start + 1):
            # ✓ marks /done-completed sessions; plain archives need no badge.
            label = "✓ " if sess.state == "completed" else ""
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
            first = f"**{idx}.** {label}*{display_name}*{lost_tag} · {age}"
            blurb = blurbs.get(sess.id) or ""
            wd = _shorten_workdir(sess.workdir) if sess.workdir else ""
            if wd:
                first += f"<br><code>{wd}</code>"
            description = blurb or "-"
            table_rows.append(
                f"| {first.replace('|', '\\|')} | "
                f"{description.replace('|', '\\|')}{RICH_TABLE_NORMAL_FONT} |"
            )
        body.append("\n".join(table_rows))

    # One button per session, paired up two-per-row (PAGE_SIZE=6 gives
    # 2+2+2). Each button's label carries the matching number so the
    # body line and button line up visually.
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, sess in enumerate(chunk, start=start + 1):
        row.append(
            InlineKeyboardButton(
                f"{idx}. {_display_name(sess)}",
                # Carry the clamped page in the callback itself rather than
                # global user_data. This remains correct for old/stale archive
                # messages and for several /archive messages in one chat.
                callback_data=f"{CB_ARC_INSPECT}{page}:{sess.id}"[:64],
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if pages > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    "◀", callback_data=f"{CB_ARC_PAGE}{(page - 1) % pages}"
                ),
                InlineKeyboardButton(
                    f"{page + 1}/{pages}", callback_data=f"{CB_ARC_PAGE}0"
                ),
                InlineKeyboardButton(
                    "▶", callback_data=f"{CB_ARC_PAGE}{(page + 1) % pages}"
                ),
            ]
        )

    if back_callback is not None:
        rows.append(
            [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=back_callback)]
        )

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

    source_backend = sess.backend
    target_backend = session_manager.agent_backend
    cross_backend = source_backend != target_backend
    resume_session_id = sess.claude_session_id or None
    initial_prompt: str | None = None
    if cross_backend:
        from ..session_import import build_import_context, import_prompt

        try:
            context_path = await asyncio.to_thread(
                build_import_context, sess, target_backend
            )
        except (OSError, ValueError) as e:
            return False, f"Could not import {source_backend} transcript: {e}"
        initial_prompt = import_prompt(context_path, source_backend)
        resume_session_id = None

    success, message, created_wname, created_wid = await tmux_manager.create_window(
        workdir,
        resume_session_id=resume_session_id,
        owner_user_id=user_id,
        backend=target_backend,
        initial_prompt=initial_prompt,
    )
    if not success:
        return False, message

    # Publish the restored window immediately. Prompts sent from Telegram now
    # queue until the real TUI input box appears (including long compaction).
    session_manager.mark_window_starting(
        created_wid,
        backend=target_backend,
        resume=resume_session_id is not None or initial_prompt is not None,
        bot=bot,
        user_id=user_id,
    )

    # Codex ``resume <id>`` keeps the same authoritative rollout id. We
    # already know everything needed to bind the window, while its SessionStart
    # hook may not run until the CLI has finished booting. Waiting 15 seconds
    # here made a normal archive restore look frozen for exactly that long.
    # Bind Codex immediately; the hook will later add transcript_path and
    # self-heal the persisted map. Claude's original id is also known, so it
    # can be published immediately and reconciled after the hook in background.
    codex_restore_published = False
    if resume_session_id and target_backend == "codex":
        from ..codex_session_io import build_session_file_path

        transcript_path = build_session_file_path(resume_session_id, workdir)
        if transcript_path is None or not transcript_path.is_file():
            session_manager.cancel_window_startup(created_wid)
            await tmux_manager.kill_window(created_wid)
            return False, "Codex rollout not found; restore was cancelled"
        try:
            await session_manager.publish_codex_restore_binding(
                sess=sess,
                user_id=user_id,
                window_id=created_wid,
                window_name=created_wname,
                transcript_path=transcript_path,
            )
        except (OSError, RuntimeError) as e:
            session_manager.cancel_window_startup(created_wid)
            await tmux_manager.kill_window(created_wid)
            logger.warning("Codex restore binding failed for %s: %s", created_wid, e)
            return False, "Could not publish Codex restore binding"
        codex_restore_published = True
    elif cross_backend:
        await session_manager.wait_for_session_map_entry(created_wid, timeout=15.0)

    # If we did a --resume, override window_state to original sid (Claude allocates a new sid for the resume).
    if resume_session_id:
        ws = session_manager.get_window_state(created_wid)
        if ws.session_id != resume_session_id:
            ws.session_id = resume_session_id
            ws.cwd = workdir
            ws.window_name = created_wname
            ws.backend = target_backend
            session_manager.save_state()
    elif cross_backend:
        ws = session_manager.get_window_state(created_wid)
        if not ws.session_id:
            session_manager.cancel_window_startup(created_wid)
            await tmux_manager.kill_window(created_wid)
            return (
                False,
                f"{target_backend} import started but no session id was captured",
            )
        if not sess.imported_from_backend:
            sess.imported_from_backend = source_backend
            sess.imported_from_session_id = sess.claude_session_id
        sess.backend = target_backend
        sess.claude_session_id = ws.session_id
        session_manager.save_state()

    if not codex_restore_published:
        session_manager.set_session_window(sess.id, created_wid)
        session_manager.set_active_session(user_id, sess.id)

    if resume_session_id and not codex_restore_published:

        async def _reconcile_resume_binding() -> None:
            try:
                await session_manager.wait_for_session_map_entry(
                    created_wid, timeout=15.0
                )
                live_ws = session_manager.get_window_state(created_wid)
                if live_ws.session_id != resume_session_id:
                    live_ws.session_id = resume_session_id
                    live_ws.cwd = workdir
                    live_ws.window_name = created_wname
                    live_ws.backend = target_backend
                    session_manager.save_state()
            except Exception as e:
                logger.warning(
                    "Background archive binding failed for %s: %s",
                    created_wid,
                    e,
                )

        asyncio.create_task(
            _reconcile_resume_binding(), name=f"archive-bind:{created_wid}"
        )

    if session_manager.get_user_settings(user_id).get("local_terminal") == "auto":
        from ..local_terminal import open_terminal_for_window

        asyncio.create_task(
            open_terminal_for_window(created_wid, user_id=user_id),
            name=f"local-terminal:{created_wid}",
        )
    note = ""
    if resume_session_id:
        note = " — if it was a large session it may compact for a minute; your first message is held until it's ready."
    elif cross_backend:
        note = (
            f" — imported from {source_backend} into a native {target_backend} session"
        )
    return True, f"Restored {sess.name or sess.id} ({created_wname}){note}"


async def idle_archive_sweep(bot: Bot, user_id: int) -> int:
    """Archive sessions that exceeded the user's selected idle TTL.

    Returns number of sessions archived.
    """
    raw_hours = session_manager.get_user_settings(user_id).get(
        "session_idle_hours", DEFAULT_IDLE_ARCHIVE_HOURS
    )
    try:
        idle_hours = int(raw_hours)
    except (TypeError, ValueError):
        idle_hours = DEFAULT_IDLE_ARCHIVE_HOURS
    if idle_hours not in IDLE_ARCHIVE_HOUR_CHOICES:
        idle_hours = DEFAULT_IDLE_ARCHIVE_HOURS
    candidates = session_manager.find_idle_to_archive(idle_hours * 3600.0)
    archived = 0
    for sess in candidates:
        await teardown_session_runtime(user_id, sess, bot)
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
