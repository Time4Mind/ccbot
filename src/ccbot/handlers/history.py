"""Message history display with pagination.

Provides history viewing functionality for Claude Code sessions:
  - _build_history_keyboard: Build inline keyboard for page navigation
  - send_history: Send or edit message history with pagination support
  - render_archived_history_pages: Format an archived session's JSONL
    transcript into Telegram-ready pages (read-only, no window needed).

Supports both full history and unread message range views.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..config import config
from ..session import Session, session_manager
from ..session_claude_io import build_session_file_path
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser
from ..transcript_types import PendingToolInfo
from .callback_data import CB_HISTORY_NEXT, CB_HISTORY_PREV
from .history_incremental import (
    IncrementalHistoryState,
    read_boundary_marker,
    read_history_delta,
)
from .history_archive import (
    render_archived_card_pages_impl,
    render_archived_history_pages_impl,
)
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


_incremental_history: dict[str, IncrementalHistoryState] = {}
_prewarm_locks: dict[str, asyncio.Lock] = {}


# Same cache shape, but keyed by ``claude_session_id`` for archived
# sessions (we render directly from the JSONL on disk — no window).
# JSONLs of archived sessions don't grow (the tmux window is dead), so
# (mtime, size) here is effectively a freeze-tag; we still verify it so
# a manually-edited file would invalidate the cache.
_archived_pages_cache: dict[str, tuple[float, int, list[str], int]] = {}

# Card-engine render cache for archived sessions (Archive → Inspect).
# Same freeze-tag shape as ``_archived_pages_cache`` but holds the pages
# produced by the live-card renderer (collapsible thinking / tool
# spoilers) rather than the flat concat. Keyed by claude_session_id;
# a page-line-budget change re-keys nothing, so we fold the budget into
# the value tuple's implicit invalidation by clearing on cache miss only
# — the budget rarely changes and a stale layout self-heals on the next
# transcript mutation. Kept separate so both renderers can coexist.
_archived_card_cache: dict[str, tuple[float, int, list[str], int]] = {}


def _session_file_path(sess: Session) -> Path | None:
    """Resolve a transcript with the session's own backend."""
    if not sess.claude_session_id:
        return None
    if sess.backend == "codex":
        from ..codex_session_io import build_session_file_path as build_codex_path

        return build_codex_path(sess.claude_session_id, sess.workdir or "")
    return build_session_file_path(sess.claude_session_id, sess.workdir or "")


def _window_file_path(window_id: str) -> Path | None:
    """Resolve a live window transcript without assuming Claude."""
    state = session_manager.get_window_state(window_id)
    if state.transcript_path:
        path = Path(state.transcript_path)
        if path.exists():
            return path
    sess = session_manager.find_session_by_window(window_id)
    if sess is not None:
        return _session_file_path(sess)
    return None


async def render_archived_card_pages(
    sess: Session, user_id: int | None = None
) -> tuple[list[str], int] | None:
    """Render an archived transcript through the live-card event pipeline."""
    return await render_archived_card_pages_impl(
        sess,
        user_id,
        session_file_path=_session_file_path,
        archived_card_cache=_archived_card_cache,
        config=config,
        logger=logger,
    )


async def render_archived_history_pages(
    sess: Session,
) -> tuple[list[str], int] | None:
    """Render an archived transcript as flat Telegram history pages."""
    return await render_archived_history_pages_impl(
        sess,
        session_file_path=_session_file_path,
        archived_pages_cache=_archived_pages_cache,
        config=config,
        logger=logger,
    )


_last_prewarm_attempt: dict[str, float] = {}

# One live fire-and-forget prewarm per window.  Keeping the task keyed by
# window is both a strong-reference lifetime guard and a single-flight gate:
# polling/card events that arrive while a large transcript is still being
# parsed reuse the in-flight work instead of opening another full-file reader.
_prewarm_tasks: dict[str, asyncio.Task[bool]] = {}


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
    running = _prewarm_tasks.get(window_id)
    if running is not None and not running.done():
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
    _prewarm_tasks[window_id] = task

    def _discard(done: asyncio.Task[bool]) -> None:
        if _prewarm_tasks.get(window_id) is done:
            _prewarm_tasks.pop(window_id, None)

    task.add_done_callback(_discard)


async def cancel_pending_prewarm(timeout: float = 2.0) -> None:
    """Cancel + drain every still-running prewarm task.

    Called once from ``post_shutdown`` before stopping the session
    monitor so pending JSONL reads don't keep running after the bot
    has nominally exited.
    """
    tasks = list(_prewarm_tasks.values())
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


def _visible_history_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply the stable visibility policy used by the full-history view."""
    visible = messages
    if not config.show_user_messages:
        visible = [message for message in visible if message["role"] == "assistant"]
    return [message for message in visible if message.get("content_type") != "tool_use"]


def _history_header(window_id: str, total: int) -> str:
    display_name = session_manager.get_display_name(window_id)
    return f"📋 [{display_name}] Messages ({total} total)"


def _history_message_blocks(messages: list[dict[str, Any]]) -> list[str]:
    """Format only message bodies; page/header assembly is handled separately."""
    quote_start = TranscriptParser.EXPANDABLE_QUOTE_START
    quote_end = TranscriptParser.EXPANDABLE_QUOTE_END
    blocks: list[str] = []
    for message in messages:
        timestamp = message.get("timestamp")
        hh_mm = ""
        if timestamp:
            try:
                time_part = timestamp.split("T")[1] if "T" in timestamp else timestamp
                hh_mm = time_part[:5]
            except (IndexError, TypeError):
                hh_mm = ""
        separator = f"───── {hh_mm} ─────" if hh_mm else "─────────────"
        text = message["text"].replace(quote_start, "").replace(quote_end, "")
        fence_lines = sum(
            1 for line in text.split("\n") if line.strip().startswith("```")
        )
        if fence_lines % 2 == 1:
            text += "\n```"
        role = message.get("role", "assistant")
        content_type = message.get("content_type", "text")
        if role == "user":
            body = f"👤 {text}"
        elif content_type == "thinking":
            body = f"∴ Thinking…\n{text}"
        else:
            body = text
        blocks.append(f"{separator}\n\n{body}")
    return blocks


def _render_cached_pages(
    window_id: str, messages: list[dict[str, Any]]
) -> tuple[list[str], int]:
    """Render an initial/rebuilt transcript snapshot into Telegram pages."""
    visible = _visible_history_messages(messages)
    total = len(visible)
    if total == 0:
        return [], 0
    text = "\n\n".join(
        [_history_header(window_id, total), *_history_message_blocks(visible)]
    )
    return list(split_message(text, max_length=4096)), total


def _append_cached_pages(
    window_id: str,
    pages: list[str],
    prior_total: int,
    added_messages: list[dict[str, Any]],
) -> tuple[list[str], int, int]:
    """Append new visible messages by reflowing only the previous last page.

    Earlier pages are immutable because JSONL transcripts are append-only. The
    last page is combined with the newly formatted blocks and split again; this
    bounds temporary allocations to roughly one Telegram page plus the delta.
    """
    visible = _visible_history_messages(added_messages)
    added_total = len(visible)
    if added_total == 0:
        return list(pages), prior_total, 0

    total = prior_total + added_total
    if not pages:
        rendered, rendered_total = _render_cached_pages(window_id, visible)
        return rendered, rendered_total, added_total

    updated = list(pages)
    header = _history_header(window_id, total)
    _old_header, separator, first_body = updated[0].partition("\n")
    first_page = header + (separator + first_body if separator else "")
    # A decimal-width transition can add one byte to a completely full first
    # page. Keep its previous count rather than publishing an invalid >4096
    # Telegram message; pagination metadata still carries the exact total.
    if len(first_page) <= 4096:
        updated[0] = first_page

    tail = "\n\n".join([updated[-1], *_history_message_blocks(visible)])
    updated[-1:] = split_message(tail, max_length=4096)
    return updated, total, added_total


async def prewarm_pages_cache(window_id: str) -> bool:
    """Build and store the rendered history pages for ``window_id`` so
    the next ``send_history`` for this window hits the cache.

    Runs the full parse-and-format pipeline silently — no Telegram
    edit / send. Idempotent: a no-op when the cache entry is already
    fresh (matches the current file mtime + size).

    Returns ``True`` when fresh pages were stored, ``False`` otherwise.
    """
    if not window_id:
        return False
    lock = _prewarm_locks.setdefault(window_id, asyncio.Lock())
    async with lock:
        try:
            fp = _window_file_path(window_id)
            if fp is None or not fp.exists():
                _incremental_history.pop(window_id, None)
                _pages_cache.pop(window_id, None)
                return False
            stat_before = fp.stat()
        except Exception as e:
            logger.debug("prewarm: stat lookup failed for %s: %s", window_id, e)
            return False

        state = _incremental_history.get(window_id)
        same_file = (
            state is not None
            and state.path == str(fp)
            and state.device == stat_before.st_dev
            and state.inode == stat_before.st_ino
        )
        if (
            same_file
            and state is not None
            and state.observed_mtime_ns == stat_before.st_mtime_ns
            and state.observed_size == stat_before.st_size
        ):
            return False

        # Replacement, truncation, or an in-place same-size rewrite invalidates
        # cumulative parser state. Ordinary append keeps the prior offset,
        # unresolved tool metadata, and already-rendered pages.
        reset = (
            state is None
            or not same_file
            or stat_before.st_size < state.offset
            or (
                stat_before.st_size == state.observed_size
                and stat_before.st_mtime_ns != state.observed_mtime_ns
            )
            or (state.rendered_total > 0 and window_id not in _pages_cache)
        )
        if not reset:
            assert state is not None
            if state.offset:
                try:
                    reset = (
                        read_boundary_marker(fp, state.offset) != state.boundary_marker
                    )
                except OSError:
                    return False
        if reset:
            start_offset = 0
            pending_tools: dict[str, PendingToolInfo] = {}
        else:
            assert state is not None
            start_offset = state.offset
            pending_tools = state.pending_tools

        try:
            added, remaining_pending, safe_offset = await asyncio.to_thread(
                read_history_delta, fp, start_offset, pending_tools
            )
            stat_after = fp.stat()
        except Exception as e:
            logger.debug("prewarm: incremental read failed for %s: %s", window_id, e)
            return False

        # A rename/replacement during the read makes the result ambiguous; do
        # not publish it.  The next polling tick starts cleanly from the new
        # inode.
        if (
            stat_after.st_dev != stat_before.st_dev
            or stat_after.st_ino != stat_before.st_ino
            or safe_offset > stat_after.st_size
        ):
            _incremental_history.pop(window_id, None)
            _pages_cache.pop(window_id, None)
            return False

        if safe_offset < stat_before.st_size:
            # The snapshot already ended in a partial line.  Remember the
            # newest metadata so unchanged polling ticks do not spin on it;
            # the writer's completion/append changes size or mtime and wakes
            # the reader again.
            observed_mtime_ns = stat_after.st_mtime_ns
            observed_size = stat_after.st_size
            cache_mtime = stat_after.st_mtime
            cache_size = stat_after.st_size
        elif safe_offset < stat_after.st_size:
            # The file grew after this read reached the original EOF.  Publish
            # the consistent snapshot we did parse, but leave its old metadata
            # in place so the next tick immediately consumes the new tail.
            observed_mtime_ns = stat_before.st_mtime_ns
            observed_size = stat_before.st_size
            cache_mtime = stat_before.st_mtime
            cache_size = stat_before.st_size
        else:
            observed_mtime_ns = stat_after.st_mtime_ns
            observed_size = stat_after.st_size
            cache_mtime = stat_after.st_mtime
            cache_size = stat_after.st_size

        cached = None if reset else _pages_cache.get(window_id)
        if cached is None:
            pages, total = _render_cached_pages(window_id, added)
            visible_added = total
        else:
            assert state is not None
            pages, total, visible_added = _append_cached_pages(
                window_id,
                cached[2],
                state.rendered_total,
                added,
            )
        new_state = IncrementalHistoryState(
            path=str(fp),
            device=stat_after.st_dev,
            inode=stat_after.st_ino,
            offset=safe_offset,
            observed_mtime_ns=observed_mtime_ns,
            observed_size=observed_size,
            boundary_marker=read_boundary_marker(fp, safe_offset),
            rendered_total=total,
            pending_tools=remaining_pending,
        )
        _incremental_history[window_id] = new_state

        if not pages:
            _pages_cache.pop(window_id, None)
            return bool(added or safe_offset != start_offset)
        _pages_cache[window_id] = (
            cache_mtime,
            cache_size,
            pages,
            total,
        )
        logger.debug(
            "prewarm: cached window=%s pages=%d total=%d added=%d visible_added=%d "
            "offset=%d/%d",
            window_id,
            len(pages),
            total,
            len(added),
            visible_added,
            safe_offset,
            stat_after.st_size,
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
            # Route every full-history miss through the same incremental,
            # per-window lock used by background polling.  This prevents a
            # switcher tap from starting the old independent full JSONL walk
            # while a prewarm is already consuming the append tail.
            await prewarm_pages_cache(window_id)
        except Exception as e:
            logger.debug("history on-demand prewarm failed: %s", e)
        try:
            fp = _window_file_path(window_id)
            if fp is not None and fp.exists():
                st = fp.stat()
                mtime = st.st_mtime
                size = st.st_size
                entry = _pages_cache.get(window_id)
                incremental = _incremental_history.get(window_id)
                same_incremental_file = (
                    incremental is not None
                    and incremental.path == str(fp)
                    and incremental.device == st.st_dev
                    and incremental.inode == st.st_ino
                )
                # Exact metadata is the common case.  If the append-only file
                # grew in the tiny gap after prewarm returned, serve its
                # consistent cached snapshot; the next polling tick consumes
                # the tail instead of forcing this user interaction into a
                # second full-file parser.
                if entry is not None and (
                    (entry[0] == mtime and entry[1] == size) or same_incremental_file
                ):
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
                fp_store = _window_file_path(window_id)
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
