"""Message history display with pagination.

Provides history viewing functionality for Claude Code sessions:
  - _build_history_keyboard: Build inline keyboard for page navigation
  - send_history: Send or edit message history with pagination support
  - render_archived_history_pages: Format an archived session's JSONL
    transcript into Telegram-ready pages (read-only, no window needed).

Supports both full history and unread message range views.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiofiles
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..config import config
from ..session import Session, session_manager
from ..session_claude_io import build_session_file_path
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser
from .callback_data import CB_HISTORY_NEXT, CB_HISTORY_PREV
from .message_sender import safe_edit, safe_reply, safe_send

logger = logging.getLogger(__name__)


# In-memory cache for rendered history pages keyed by ``window_id``.
# Switcher taps re-parse the entire JSONL on every tap — for a 1.5k-
# message transcript that's ~800 ms wall-clock, and the user sees the
# previously-painted content "stuck" for almost a second before the new
# history lands. Cache the rendered pages so a repeat tap (or rapid
# back-and-forth between sessions) only pays the Telegram API round-
# trip (~150 ms).
#
# Cached entry holds (file_mtime, file_size, pages_list, total_count).
# Invalidated automatically when the transcript file grows or its mtime
# advances — i.e., on the next claude event for that session. The full
# (un-byte-ranged) case is the only one cached; unread-range reads are
# rare and parameterised, so they go through the slow path.
_pages_cache: dict[str, tuple[float, int, list[str], int]] = {}


# Same cache shape, but keyed by ``claude_session_id`` for archived
# sessions (we render directly from the JSONL on disk — no window).
# JSONLs of archived sessions don't grow (the tmux window is dead), so
# (mtime, size) here is effectively a freeze-tag; we still verify it so
# a manually-edited file would invalidate the cache.
_archived_pages_cache: dict[str, tuple[float, int, list[str], int]] = {}


async def render_archived_history_pages(
    sess: Session,
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
    fp = build_session_file_path(sid, sess.workdir)
    if fp is None or not fp.exists():
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

    cached = _archived_pages_cache.get(sid)
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
    _archived_pages_cache[sid] = (st.st_mtime, st.st_size, list(pages), total)
    return list(pages), total


_last_prewarm_attempt: dict[str, float] = {}

# Live, fire-and-forget prewarm tasks. We keep a strong reference so
# the event loop doesn't garbage-collect them mid-run (CPython will
# silently drop bare ``asyncio.create_task`` results once the local
# binding goes away), and so ``cancel_pending_prewarm()`` can drain
# them on shutdown — otherwise asyncio logs ``Task was destroyed but
# it is pending!`` and an in-flight JSONL read can race with the
# session monitor's final state save.
_prewarm_tasks: set[asyncio.Task[bool]] = set()


def kick_prewarm(window_id: str, min_interval: float = 3.0) -> None:
    """Schedule a background prewarm of the pages cache for ``window_id``.

    Fire-and-forget: returns immediately; the actual JSONL parse runs
    in a background task. Throttled to at most one attempt per
    ``min_interval`` seconds per window so the streaming-rate keyboard
    renders don't trigger a re-parse of the whole transcript on every
    event.

    Use before any keyboard build that needs the cached page count
    (e.g. the live-card pagination counter): the counter shows up
    once the background task lands, and stays stable across subsequent
    renders even when the cache goes stale relative to the growing JSONL.
    """
    import asyncio
    import time

    if not window_id:
        return
    now = time.monotonic()
    last = _last_prewarm_attempt.get(window_id, 0.0)
    if now - last < min_interval:
        return
    _last_prewarm_attempt[window_id] = now
    try:
        task = asyncio.create_task(prewarm_pages_cache(window_id))
    except RuntimeError:
        # No running loop — caller is in sync context outside the bot.
        # Skip; another path (status polling, the next callback) will
        # populate the cache eventually.
        return
    _prewarm_tasks.add(task)
    task.add_done_callback(_prewarm_tasks.discard)


async def cancel_pending_prewarm(timeout: float = 2.0) -> None:
    """Cancel + drain every still-running prewarm task.

    Called once from ``post_shutdown`` before stopping the session
    monitor so pending JSONL reads don't keep running after the bot
    has nominally exited.
    """
    tasks = list(_prewarm_tasks)
    if not tasks:
        return
    for t in tasks:
        if not t.done():
            t.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            "prewarm shutdown drain timed out after %ss with %d tasks pending",
            timeout,
            sum(1 for t in tasks if not t.done()),
        )
    finally:
        _prewarm_tasks.clear()


async def prewarm_pages_cache(window_id: str) -> bool:
    """Build and store the rendered history pages for ``window_id`` so
    the next ``send_history`` for this window hits the cache.

    Runs the full parse-and-format pipeline silently — no Telegram
    edit / send. Idempotent: a no-op when the cache entry is already
    fresh (matches the current file mtime + size).

    Returns ``True`` when fresh pages were stored, ``False`` otherwise.
    """
    from ..session_claude_io import build_session_file_path

    try:
        state = session_manager.get_window_state(window_id)
        if not state.session_id or not state.cwd:
            return False
        fp = build_session_file_path(state.session_id, state.cwd)
        if fp is None or not fp.exists():
            return False
        st = fp.stat()
        entry = _pages_cache.get(window_id)
        if entry is not None and entry[0] == st.st_mtime and entry[1] == st.st_size:
            return False  # already fresh
    except Exception as e:
        logger.debug("prewarm: stat lookup failed for %s: %s", window_id, e)
        return False

    try:
        messages, total = await session_manager.get_recent_messages(window_id)
    except Exception as e:
        logger.debug("prewarm: get_recent_messages failed for %s: %s", window_id, e)
        return False
    if total == 0:
        return False

    if not config.show_user_messages:
        messages = [m for m in messages if m["role"] == "assistant"]
    # Drop ``tool_use`` entries — TranscriptParser emits them as soon
    # as the streaming API sees the call, BEFORE the tool_result lands
    # on disk. The matching ``tool_result`` entry that arrives later
    # carries the same ``**Tool**(args)`` header AND the body/diff —
    # rendering both shows each tool call twice (bare name first, then
    # the same name with the body). Worse, in a fast-moving session
    # the JSONL can flush tool_use rows with no matching tool_result
    # yet, so the cache snapshots a long tail of empty tool names.
    messages = [m for m in messages if m.get("content_type") != "tool_use"]
    total = len(messages)
    if total == 0:
        return False

    display_name = session_manager.get_display_name(window_id)
    _start = TranscriptParser.EXPANDABLE_QUOTE_START
    _end = TranscriptParser.EXPANDABLE_QUOTE_END
    lines = [f"📋 [{display_name}] Messages ({total} total)"]
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
        msg_text = msg["text"].replace(_start, "").replace(_end, "")
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
    _pages_cache[window_id] = (st.st_mtime, st.st_size, list(pages), total)
    logger.debug(
        "prewarm: cached window=%s pages=%d total=%d", window_id, len(pages), total
    )
    return True


def _build_history_keyboard(
    window_id: str,
    page_index: int,
    total_pages: int,
    start_byte: int = 0,
    end_byte: int = 0,
) -> InlineKeyboardMarkup | None:
    """Build inline keyboard for history pagination.

    Callback format: hp:<page>:<window_id>:<start>:<end> or hn:<page>:<window_id>:<start>:<end>
    When start=0 and end=0, it means full history (no byte range filter).
    """
    if total_pages <= 1:
        return None

    buttons = []
    if page_index > 0:
        cb_data = (
            f"{CB_HISTORY_PREV}{page_index - 1}:{window_id}:{start_byte}:{end_byte}"
        )
        buttons.append(
            InlineKeyboardButton(
                "◀ Older",
                callback_data=cb_data[:64],
            )
        )

    buttons.append(
        InlineKeyboardButton(f"{page_index + 1}/{total_pages}", callback_data="noop")
    )

    if page_index < total_pages - 1:
        cb_data = (
            f"{CB_HISTORY_NEXT}{page_index + 1}:{window_id}:{start_byte}:{end_byte}"
        )
        buttons.append(
            InlineKeyboardButton(
                "Newer ▶",
                callback_data=cb_data[:64],
            )
        )

    return InlineKeyboardMarkup([buttons])


async def send_history(
    target: Any,
    window_id: str,
    offset: int = -1,
    edit: bool = False,
    *,
    start_byte: int = 0,
    end_byte: int = 0,
    user_id: int | None = None,
    bot: Bot | None = None,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> None:
    """Send or edit message history for a window's session.

    Args:
        target: Message object (for reply) or CallbackQuery (for edit).
        window_id: Tmux window ID (resolved to session via window_states).
        offset: Page index (0-based). -1 means last page (for full history)
                or first page (for unread range).
        edit: If True, edit existing message instead of sending new one.
        start_byte: Start byte offset (0 = from beginning).
        end_byte: End byte offset (0 = to end of file).
        user_id: User ID for updating read offset (required for unread mode).
        bot: Bot instance for direct send mode (when edit=False and bot is provided).
    """
    display_name = session_manager.get_display_name(window_id)
    # Determine if this is unread mode (specific byte range)
    is_unread = start_byte > 0 or end_byte > 0
    logger.debug(
        "send_history: window_id=%s (%s), offset=%d, is_unread=%s, byte_range=%d-%d",
        window_id,
        display_name,
        offset,
        is_unread,
        start_byte,
        end_byte,
    )

    # Cache fast-path: full-history reads (no byte range) come through
    # the switcher-tap hot path. Try to serve the prebuilt pages instead
    # of re-parsing the JSONL on every tap. Cache key is the transcript
    # file's (mtime, size) — claude appends new entries strictly
    # forward, so a single ``stat()`` is enough to invalidate.
    #
    # NB: the lookup uses ``build_session_file_path`` (pure path math)
    # rather than ``resolve_session_for_window`` — the latter re-walks
    # the entire JSONL to refresh summary/token stats, which would
    # negate the cache's whole point.
    if not is_unread:
        cached_pages: list[str] | None = None
        cached_total = 0
        try:
            from ..session_claude_io import build_session_file_path

            state = session_manager.get_window_state(window_id)
            fp: Path | None = None
            if state.session_id and state.cwd:
                fp = build_session_file_path(state.session_id, state.cwd)
            if fp is not None and fp.exists():
                st = fp.stat()
                mtime = st.st_mtime
                size = st.st_size
                entry = _pages_cache.get(window_id)
                if entry is not None and entry[0] == mtime and entry[1] == size:
                    cached_pages = entry[2]
                    cached_total = entry[3]
                    logger.debug(
                        "send_history cache HIT window=%s pages=%d total=%d",
                        window_id,
                        len(cached_pages),
                        cached_total,
                    )
        except Exception as e:
            logger.debug("history cache lookup failed: %s", e)

        if cached_pages is not None:
            if offset < 0:
                page_index = len(cached_pages) - 1
            else:
                page_index = max(0, min(offset, len(cached_pages) - 1))
            text = cached_pages[page_index]
            keyboard = _build_history_keyboard(
                window_id, page_index, len(cached_pages), start_byte, end_byte
            )
            if extra_rows:
                existing_rows = (
                    list(keyboard.inline_keyboard) if keyboard is not None else []
                )
                keyboard = InlineKeyboardMarkup(
                    existing_rows + [list(r) for r in extra_rows]
                )
            if edit:
                await safe_edit(target, text, reply_markup=keyboard)
            elif bot is not None and user_id is not None:
                await safe_send(bot, user_id, text, reply_markup=keyboard)
            else:
                await safe_reply(target, text, reply_markup=keyboard)
            return

    messages, total = await session_manager.get_recent_messages(
        window_id,
        start_byte=start_byte,
        end_byte=end_byte if end_byte > 0 else None,
    )

    if total == 0:
        if is_unread:
            text = f"📬 [{display_name}] No unread messages."
        else:
            text = f"📋 [{display_name}] No messages yet."
        keyboard = None
    else:
        _start = TranscriptParser.EXPANDABLE_QUOTE_START
        _end = TranscriptParser.EXPANDABLE_QUOTE_END

        # Filter messages based on config
        if config.show_user_messages:
            # Keep both user and assistant messages
            pass
        else:
            # Filter to assistant messages only
            messages = [m for m in messages if m["role"] == "assistant"]
        # Drop ``tool_use`` entries — see ``prewarm_pages_cache`` for the
        # full rationale. Short version: the parser emits a separate
        # ``tool_use`` entry the moment the streaming API sees the call,
        # then emits another ``tool_result`` entry once the body lands;
        # both carry the same ``**Tool**(args)`` header, so keeping the
        # first one only adds a content-less row above its own result.
        messages = [m for m in messages if m.get("content_type") != "tool_use"]
        total = len(messages)
        if total == 0:
            if is_unread:
                text = f"📬 [{display_name}] No unread messages."
            else:
                text = f"📋 [{display_name}] No messages yet."
            keyboard = None
            if edit:
                await safe_edit(target, text, reply_markup=keyboard)
            elif bot is not None and user_id is not None:
                await safe_send(bot, user_id, text, reply_markup=keyboard)
            else:
                await safe_reply(target, text, reply_markup=keyboard)
            # Update offset even if no assistant messages
            if user_id is not None and end_byte > 0:
                session_manager.update_user_window_offset(user_id, window_id, end_byte)
            return

        if is_unread:
            header = f"📬 [{display_name}] {total} unread messages"
        else:
            header = f"📋 [{display_name}] Messages ({total} total)"

        lines = [header]
        for msg in messages:
            # Format timestamp as HH:MM
            ts = msg.get("timestamp")
            if ts:
                try:
                    # ISO format: 2024-01-15T14:32:00.000Z
                    time_part = ts.split("T")[1] if "T" in ts else ts
                    hh_mm = time_part[:5]  # "14:32"
                except (IndexError, TypeError):
                    hh_mm = ""
            else:
                hh_mm = ""

            # Add separator with time
            if hh_mm:
                lines.append(f"───── {hh_mm} ─────")
            else:
                lines.append("─────────────")

            # Format message content
            msg_text = msg["text"]
            content_type = msg.get("content_type", "text")
            msg_role = msg.get("role", "assistant")

            # Strip expandable quote sentinels for history view
            msg_text = msg_text.replace(_start, "").replace(_end, "")

            # Balance triple-backtick code fences inside this entry so
            # an unclosed ``` can't bleed into the next entry and turn
            # the rest of the page into one giant <pre>.
            #
            # We must use the SAME line-start check that ``split_message``
            # uses to track ``in_code_block`` — otherwise the per-entry
            # balance and the page-split balance disagree, and the disagreement
            # leaks an open fence across the chunk boundary. The buggy
            # naïve ``text.count("```")`` counted any occurrence of three
            # backticks anywhere (incl. ``+```` in unified-diff hunks
            # and ``"``​`"`` inside code that references markdown), so
            # entries with diffs would be flagged "odd" and gain a stray
            # closing fence — the next entry then started inside a code
            # block from the parser's point of view, and the whole rest of
            # the page rendered as ``<pre>``.
            fence_lines = sum(
                1 for ln in msg_text.split("\n") if ln.strip().startswith("```")
            )
            if fence_lines % 2 == 1:
                msg_text = msg_text + "\n```"

            # Add prefix based on role/type
            if msg_role == "user":
                # User message with emoji prefix (no newline)
                lines.append(f"👤 {msg_text}")
            elif content_type == "thinking":
                # Thinking prefix to match real-time format
                lines.append(f"∴ Thinking…\n{msg_text}")
            else:
                lines.append(msg_text)
        full_text = "\n\n".join(lines)
        pages = split_message(full_text, max_length=4096)

        # Stash the freshly-built pages so the next tap on the same
        # window skips the parse step. We only cache the full-history
        # case — unread-range reads are parameterised and rare.
        if not is_unread:
            try:
                from ..session_claude_io import build_session_file_path

                state = session_manager.get_window_state(window_id)
                fp_store: Path | None = None
                if state.session_id and state.cwd:
                    fp_store = build_session_file_path(state.session_id, state.cwd)
                if fp_store is not None and fp_store.exists():
                    st = fp_store.stat()
                    _pages_cache[window_id] = (
                        st.st_mtime,
                        st.st_size,
                        list(pages),
                        total,
                    )
            except Exception as e:
                logger.debug("history cache store failed: %s", e)

        # Default to last page (newest messages) for both history and unread
        if offset < 0:
            offset = len(pages) - 1
        page_index = max(0, min(offset, len(pages) - 1))
        text = pages[page_index]
        keyboard = _build_history_keyboard(
            window_id, page_index, len(pages), start_byte, end_byte
        )
        logger.debug(
            "send_history result: %d messages, %d pages, serving page %d",
            total,
            len(pages),
            page_index,
        )

    # Append caller-supplied extra keyboard rows (used by the inline Menu's
    # "History" sub-screen to add a Menu-grid below pagination).
    if extra_rows:
        existing_rows = list(keyboard.inline_keyboard) if keyboard is not None else []
        keyboard = InlineKeyboardMarkup(existing_rows + [list(r) for r in extra_rows])

    if edit:
        await safe_edit(target, text, reply_markup=keyboard)
    elif bot is not None and user_id is not None:
        # Direct send mode (for unread catch-up after window switch)
        await safe_send(bot, user_id, text, reply_markup=keyboard)
    else:
        await safe_reply(target, text, reply_markup=keyboard)

    # Update user's read offset after viewing unread
    if is_unread and user_id is not None and end_byte > 0:
        session_manager.update_user_window_offset(user_id, window_id, end_byte)
