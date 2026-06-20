"""Live-card notifications for the active session.

A "card" is a single Telegram message that the bot keeps editMessageText-
updating as Claude emits tool calls, thinking blocks, and text chunks.
Only the **active** session paints its card to chat; background sessions
go through ``handlers.bg_status`` and surface as a compact panel at the
bottom of the active card.

A fresh card opens (a new TG message is sent) on:

  - long pause: previous card sat idle for >= STALE_CARD_SECONDS
  - ``repost_card`` (always-repost behaviour: every user-msg replaces
    the card with a fresh one below)
  - first event of a new session

Within a single card, content is paginated. Each ``Event`` with
``is_page_break=True`` (currently end_turn assistant text) becomes
the top of a new page; everything preceding it goes on the previous
page. Default focus = the page anchored to the latest answer.

The header line carries:

  ``<emoji> *<name>* [<quota>] · <state> · HH:MM``

— where HH:MM is the time of the last claude event so the user can
tell at a glance whether the card is fresh or has been quiet.

When a tool_result arrives matching a previous tool_use, the existing
tool_use Event is mutated in place (``completed_at`` set, body replaced
with the result), so the ``▷`` line flips to ``✓`` (or ``✗`` on error)
and the elapsed timer is replaced with the start-time HH:MM.

Module layout
-------------
This module is the lifecycle / facade layer. The pure model + render
helpers live in ``handlers.card_model``; the stateless kb-mode keyboard
and pane-capture helpers live in ``handlers.kb_mode``. Both are
re-exported below so existing
``from ccbot.handlers.notifications import X`` and ``notifications.X``
references resolve unchanged. The module-global card registries
(``_cards`` / ``_card_locks`` / ``_repost_intent`` / ``_msg_to_session``)
and all the stateful orchestration that mutates them stay here.
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter

from ..config import config
from ..session import Session, session_manager
from ..session_monitor import NewMessage
from .card_model import (
    CARD_HARD_LIMIT,
    CARD_MAX_EVENTS,
    CARD_PAGE_BUDGET,
    CARD_PAGE_LINES_DEFAULT,
    CARD_PAGE_LINES_OVERSHOOT,
    CARD_SEED_TURNS,
    SPOILER_MAX_LINES,
    STALE_CARD_SECONDS,
    CardState,
    Event,
    _apply_tool_result,
    _build_event,
    _card_is_busy,
    _chunk_final_text,
    _count_lines,
    _duplicate_of_seeded,
    _estimate_md_v2_size,
    _extract_expquote_inner,
    _format_hhmmss,
    _is_in_flight,
    _is_stale,
    _latest_inflight_idx,
    _rechunk_oversized_finals_inplace,
    _render_card,
    _resolve_line_budget,
    _resolved_page_idx,
    _spoiler_body,
    _split_page_by_budget,
    _split_tool_text,
    _strip_for_card,
    _trim,
    _trim_page_events,
    card_page_info,
    paginate_events,
    paginate_events_for_card,
    render_event,
    render_page,
)
from .kb_mode import _capture_pane_png, build_kb_mode_keyboard
from .menu import build_footer_keyboard
from .message_sender import safe_send
from .switcher import session_emoji
from .tg_format import Attachment, split_overflow

logger = logging.getLogger(__name__)

# Re-export model / kb-mode names so existing
# ``from ccbot.handlers.notifications import X`` callers and the e2e
# tests' ``notifications.X`` references keep resolving unchanged.
__all__ = [
    "CARD_HARD_LIMIT",
    "CARD_MAX_EVENTS",
    "CARD_PAGE_BUDGET",
    "CARD_PAGE_LINES_DEFAULT",
    "CARD_PAGE_LINES_OVERSHOOT",
    "CARD_SEED_TURNS",
    "SPOILER_MAX_LINES",
    "STALE_CARD_SECONDS",
    "CardState",
    "Event",
    "_apply_tool_result",
    "_build_event",
    "_capture_pane_png",
    "_card_is_busy",
    "_chunk_final_text",
    "_count_lines",
    "_duplicate_of_seeded",
    "_estimate_md_v2_size",
    "_extract_expquote_inner",
    "_format_hhmmss",
    "_is_in_flight",
    "_is_stale",
    "_latest_inflight_idx",
    "_rechunk_oversized_finals_inplace",
    "_render_card",
    "_resolve_line_budget",
    "_resolved_page_idx",
    "_spoiler_body",
    "_split_page_by_budget",
    "_split_tool_text",
    "_strip_for_card",
    "_trim",
    "_trim_page_events",
    "build_kb_mode_keyboard",
    "card_page_info",
    "paginate_events",
    "paginate_events_for_card",
    "render_event",
    "render_page",
]


# Per-(user, session.id) card state.
_cards: dict[tuple[int, str], CardState] = {}

# Per-(user, session.id) async lock. Acquired by every code path that
# may decide to ``_send_card`` (spawn a fresh card msg) so two
# concurrent paths can't both observe ``state.msg_id is None`` and
# both spawn — the artefact behind Task #50 ("2 messages in wrong
# order after switcher / new card"). Edit-only paths that never spawn
# (refresh_panel, card_timer_loop ticks, _deferred_edit) don't take
# the lock — at worst they race a spawn and either succeed against
# the freshly-spawned msg or hit lost-carrier and reset msg_id, which
# is recovered on the next event.
_card_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _card_lock(user_id: int, session_id: str) -> asyncio.Lock:
    """Get-or-create the spawn-serialization lock for one card."""
    key = (user_id, session_id)
    lock = _card_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _card_locks[key] = lock
    return lock


# Reverse lookup so reply-quote can route a one-shot user message to the
# session that owns the message being replied to. Capped via FIFO eviction.
_MSG_REGISTRY_LIMIT = 2000
_msg_to_session: dict[tuple[int, int], str] = {}


def _register_msg(user_id: int, message_id: int, session_id: str) -> None:
    """Remember which session a bot message belongs to for reply-quote routing."""
    key = (user_id, message_id)
    # Best-effort eviction: drop ~10% of the oldest entries when the cap
    # is hit. dict preserves insertion order in CPython 3.7+.
    if len(_msg_to_session) >= _MSG_REGISTRY_LIMIT and key not in _msg_to_session:
        drop = max(1, _MSG_REGISTRY_LIMIT // 10)
        for k in list(_msg_to_session.keys())[:drop]:
            _msg_to_session.pop(k, None)
    _msg_to_session[key] = session_id


def lookup_session_for_message(user_id: int, message_id: int) -> str | None:
    """Resolve a Telegram message id back to the Session.id it represents."""
    return _msg_to_session.get((user_id, message_id))


def reset_card_msg_id_for_user(user_id: int) -> None:
    """Drop the msg_id for every card of ``user_id`` so the next event
    creates a fresh msg of the (possibly changed) correct type.

    Called when the user toggles ``card_inline_screenshots`` — the new
    msg type (photo+caption vs text) cannot be reached via editMessage*
    on the old msg, so we orphan the old artefact and spawn a new one.
    """
    for (uid, _sid), state in _cards.items():
        if uid != user_id:
            continue
        state.msg_id = None
        state.is_photo_msg = False
        state.last_rendered = ""
        state.last_pane_hash = ""
        state.last_photo_edit_ts = 0.0


def _inline_screens_enabled(user_id: int | None) -> bool:
    """Read the ``card_inline_screenshots`` user-setting (default False)."""
    if user_id is None:
        return False
    settings = session_manager.get_user_settings(user_id)
    return bool(settings.get("card_inline_screenshots", False))


def has_pending_kb(user_id: int, session_id: str) -> tuple[bool, bool]:
    """Return (has_prompt, in_kb_mode) for the (user, session) card.

    Public alternative to peeking at ``_cards``. ``has_prompt=True`` means
    a prompt is pending; ``in_kb_mode`` reflects whether the card msg is
    currently displaying kb-mode view vs the regular card.
    """
    state = _cards.get((user_id, session_id))
    if state is None:
        return False, False
    return bool(state.kb_prompt), state.in_kb_mode


async def enter_kb_mode(
    bot: Bot,
    user_id: int,
    sess: Session,
    prompt_content: str,
    ui_name: str,
) -> None:
    """Flip the active session's card msg into kb-mode view.

    Edits the existing card msg (or creates one if missing) so its body
    shows the prompt content and its keyboard is the kb-mode 3×3 grid +
    [Back][+ new][≡ Menu]. State is marked ``in_kb_mode=True`` and
    ``kb_prompt`` snapshot so subsequent paints stay consistent.

    No-op if state is already in kb-mode with the same prompt — avoids
    pointless edits when status_polling re-detects the prompt each poll.
    """
    state = get_card_state(user_id, sess)
    # Short-circuit ONLY when the kb-mode card is actually present in
    # chat. After ``close_card_view`` (Shot tap) ``msg_id`` is None but
    # ``in_kb_mode`` stays True — without the ``msg_id is not None``
    # check, subsequent status_polling re-detections of the same UI
    # would no-op and the kb-mode card would never be re-spawned.
    if (
        state.in_kb_mode
        and state.kb_prompt == prompt_content
        and state.msg_id is not None
    ):
        return
    state.kb_prompt = prompt_content
    state.kb_ui_name = ui_name
    state.in_kb_mode = True
    # kb-mode is an interrupt: claude is BLOCKED waiting for the user's
    # answer. If the user happens to be on Menu / List / Settings /
    # History on the same carrier (``in_menu_view=True``), ``_edit_card``
    # would short-circuit and the kb keyboard would never surface — the
    # user only saw it appear after tapping Shot, which dropped
    # ``msg_id=None`` and re-spawned a fresh card via ``_send_card``.
    # Clearing the flag here lets ``_edit_card`` repaint the carrier
    # with the kb prompt; the menu navigation is preempted because the
    # session can't proceed without the user's input anyway.
    state.in_menu_view = False
    if not sess.window_id:
        return
    text = _render_card(sess, state, user_id=user_id)
    kb = build_kb_mode_keyboard(user_id, sess.window_id, ui_name=ui_name)
    # Spawn-serialization (Task #50): a parallel ``update_session_card``
    # could otherwise observe ``msg_id is None`` during ``_send_card``
    # and spawn its own card too.
    async with _card_lock(user_id, sess.id):
        if state.msg_id is None:
            await _send_card(bot, user_id, sess, state, text=text, reply_markup=kb)
        else:
            await _edit_card(bot, user_id, state, text=text, reply_markup=kb)
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
    logger.info(
        "kb_mode entered user=%d sess=%s ui=%s prompt_len=%d",
        user_id,
        sess.id,
        ui_name,
        len(prompt_content),
        extra={
            "event": "kb_mode_entered",
            "user_id": user_id,
            "session_id": sess.id,
            "ui_name": ui_name,
            "prompt_len": len(prompt_content),
        },
    )


async def exit_kb_mode(
    bot: Bot,
    user_id: int,
    sess: Session,
    *,
    clear_pending: bool = False,
) -> None:
    """Flip the card back from kb-mode to regular view.

    ``clear_pending=False`` (default) — user tapped Back. ``kb_prompt``
    is KEPT so the Resume button shows up in the footer. Tapping Resume
    re-enters kb-mode with the same prompt.

    ``clear_pending=True`` — claude moved past the prompt (terminal_parser
    no longer detects it, after double-poll confirm) OR user explicitly
    acted via a kb key. Wipe both ``in_kb_mode`` and ``kb_prompt`` so
    the Resume button disappears.
    """
    state = _cards.get((user_id, sess.id))
    if state is None:
        return
    was_in_kb = state.in_kb_mode
    state.in_kb_mode = False
    if clear_pending:
        state.kb_prompt = ""
        state.kb_ui_name = ""
    if state.msg_id is None or not was_in_kb:
        return
    text = _render_card(sess, state, user_id=user_id)
    if await _edit_card(bot, user_id, state, text=text):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
    logger.info(
        "kb_mode exited user=%d sess=%s cleared=%s",
        user_id,
        sess.id,
        clear_pending,
        extra={
            "event": "kb_mode_exited",
            "user_id": user_id,
            "session_id": sess.id,
            "clear_pending": clear_pending,
        },
    )


def get_card_state(user_id: int, sess: Session) -> CardState:
    return _cards.setdefault((user_id, sess.id), CardState())


async def _seed_events_from_jsonl(
    sess: Session, max_turns: int = CARD_SEED_TURNS
) -> list[Event]:
    """Build a list[Event] from the session's JSONL transcript.

    Pulls the last ``max_turns`` end-of-turn boundaries so the card has
    visible history after a bot restart (when in-memory ``state.events``
    is empty). Returns ``[]`` on any failure — caller just continues
    with an empty card.

    ``max_turns`` defaults to the module constant but is overridden by
    ``_ensure_seeded`` from the user's ``card_history`` setting.
    """
    if not sess.window_id:
        return []
    # Derive the transcript path by pure path math instead of
    # ``resolve_session_for_window`` — the latter fully walks the JSONL
    # just to refresh summary/token stats we don't use here, then we read
    # the file again below. On a multi-MB resumed transcript that wasted
    # walk costs >1s. Same fast-path the /history cache already uses.
    from ..session_claude_io import build_session_file_path

    state = session_manager.get_window_state(sess.window_id)
    if not state.session_id or not state.cwd:
        return []
    fp = build_session_file_path(state.session_id, state.cwd)
    if fp is None or not fp.exists():
        return []
    file_path = str(fp)
    import json as _json
    from pathlib import Path as _Path

    from ..transcript_parser import TranscriptParser

    try:
        raw = _Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("seed: read JSONL %s failed: %s", file_path, e)
        return []
    raw_entries: list[dict[str, object]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            raw_entries.append(_json.loads(line))
        except Exception:
            continue
    try:
        parsed_list, _ = TranscriptParser.parse_entries(raw_entries, pending_tools=None)
    except Exception as e:
        logger.debug("seed: parse_entries failed: %s", e)
        return []

    # Walk backwards collecting indices of end_turn boundaries (final
    # assistant text). Keep only entries from the last CARD_SEED_TURNS
    # boundaries — earlier history stays in JSONL for /screenshot or
    # other history paths.
    end_turn_idxs: list[int] = []
    for i in range(len(parsed_list) - 1, -1, -1):
        p = parsed_list[i]
        if (
            getattr(p, "role", "") == "assistant"
            and getattr(p, "content_type", "") == "text"
            and getattr(p, "stop_reason", "")
            in ("end_turn", "stop_sequence", "max_tokens")
        ):
            end_turn_idxs.append(i)
            if len(end_turn_idxs) >= max_turns:
                break
    if end_turn_idxs:
        start_idx = end_turn_idxs[-1]
        # Pull a few entries back from start_idx so the user message that
        # triggered the oldest kept turn is visible at the top.
        start_idx = max(0, start_idx - 4)
    else:
        start_idx = max(0, len(parsed_list) - 80)
    tail = parsed_list[start_idx:]

    # Convert ParsedEntry → NewMessage → Event. tool_results fold into
    # matching tool_use via _apply_tool_result; on miss they append.
    pseudo_state = CardState()
    events = pseudo_state.events
    for p in tail:
        ct = getattr(p, "content_type", "text")
        msg = NewMessage(
            session_id="seed",
            text=getattr(p, "text", "") or "",
            is_complete=True,
            content_type=ct,
            tool_use_id=getattr(p, "tool_use_id", None),
            role=getattr(p, "role", "assistant"),
            tool_name=getattr(p, "tool_name", None),
            image_data=getattr(p, "image_data", None),
            stop_reason=getattr(p, "stop_reason", None),
            timestamp=getattr(p, "timestamp", "") or "",
        )
        ev = _build_event(msg)
        if ct == "tool_result" and _apply_tool_result(pseudo_state, ev):
            continue
        events.append(ev)
    return events


def _transcript_mtime(sess: Session) -> float:
    """Return the mtime (epoch seconds) of the session's JSONL transcript,
    or -1.0 if the path can't be resolved / the file is missing.

    Cheap (single ``stat``) — used by ``_ensure_seeded`` to gate empty-seed
    retries on a restored session without re-parsing the whole transcript.
    """
    if not sess.window_id:
        return -1.0
    from ..session_claude_io import build_session_file_path

    state = session_manager.get_window_state(sess.window_id)
    if not state.session_id or not state.cwd:
        return -1.0
    fp = build_session_file_path(state.session_id, state.cwd)
    if fp is None:
        return -1.0
    try:
        return fp.stat().st_mtime
    except OSError:
        return -1.0


async def _ensure_seeded(user_id: int, sess: Session, state: CardState) -> None:
    """Seed ``state.events`` from JSONL on first access after restart.

    No-op when events already exist. Latches ``seed_attempted`` only on a
    *successful* (non-empty) seed: a freshly restored (``claude --resume``)
    session builds its card before claude has flushed the resumed transcript
    to disk, so an early read returns [] — latching then would block the
    seed forever and the history would never reach the card. An empty read
    instead leaves the flag clear and retries on a later event, gated on the
    transcript mtime advancing (``state.seed_mtime``) so a burst of events
    during the resume window doesn't re-parse a multi-MB JSONL each time. A
    wipe site that wants a re-seed clears ``seed_attempted`` + ``seed_mtime``
    (see ``CardState.seed_attempted``).
    """
    if state.events:
        return
    if state.seed_attempted:
        return
    mtime = _transcript_mtime(sess)
    if mtime >= 0.0 and mtime == state.seed_mtime:
        # Nothing new on disk since the last empty attempt — skip the
        # re-parse and wait for the transcript to grow.
        return
    state.seed_mtime = mtime
    # User-settable depth — Settings → Card history (10/20/50/100).
    try:
        max_turns = int(
            session_manager.get_user_settings(user_id).get(
                "card_history", CARD_SEED_TURNS
            )
        )
    except (TypeError, ValueError):
        max_turns = CARD_SEED_TURNS
    seeded = await _seed_events_from_jsonl(sess, max_turns=max_turns)
    if seeded:
        state.events = seeded
        state.seed_attempted = True
        logger.info(
            "card_seeded user=%d sess=%s events=%d",
            user_id,
            sess.id,
            len(seeded),
            extra={
                "event": "card_seeded",
                "user_id": user_id,
                "session_id": sess.id,
                "events": len(seeded),
            },
        )


def _should_buffer(user_id: int, session_id: str, state: CardState) -> bool:
    """Return True when the live card must buffer events instead of
    rendering. Four reasons:

    1. The user has the carrier on a Menu / sub-screen
       (``state.in_menu_view`` — set by ``pause_card_view`` /
       ``transfer_card_to_carrier``, cleared by ``resume_card_view`` /
       ``release_card_message`` / ``detach_paused_cards_at_message``).
    2. The session is currently a background one for this user
       (``get_active_session(user_id).id != session_id``). Computed
       live, NOT stored — a session that's briefly bg and then active
       again recovers without help. (Earlier this was implemented as a
       sticky ``state.in_menu_view = True`` inside update_session_card;
       the flag never got cleared on becoming active again, so the card
       stayed paused forever — silent until the next typed message
       woke ``resume_card_view``. This helper makes the bg check live
       so that class of bug can't reoccur.)
    3. ``text_handler`` has signalled an imminent ``repost_card`` for
       this (user, session) via ``begin_repost_intent``. Without the
       buffer, claude's first reply event after the user's typed text
       races against the repost and both ``update_session_card`` and
       ``repost_card`` end up calling ``_send_card`` — two cards land
       in chat (or one survives + claude's first event is lost when
       ``delete_message`` succeeds on a card that already had content).
       Buffering defers the rendering until ``end_repost_intent``
       cleared the flag; events accumulate in ``state.events`` and
       drain into the freshly-reposted card on the next render.
    4. The card is in kb-mode (``state.in_kb_mode``). Without this,
       a stray streaming event (assistant text emitted right before the
       AskUserQuestion lands, e.g.) would trigger ``_edit_card`` with
       the default footer keyboard — overwriting the kb keyboard the
       user needs to act on. Buffer until ``exit_kb_mode`` clears the
       flag; the drained events land on the next post-prompt render.
    """
    if state.in_menu_view:
        return True
    if state.in_kb_mode:
        return True
    if (user_id, session_id) in _repost_intent:
        return True
    active = session_manager.get_active_session(user_id)
    return active is None or active.id != session_id


# (user_id, session_id) pairs for which ``text_handler`` is mid-dispatch
# and will call ``repost_card`` shortly. While the pair is in this set,
# ``update_session_card`` buffers events instead of spawning a fresh
# card — see ``_should_buffer`` reason 3. Populated/cleared by
# ``begin_repost_intent`` / ``end_repost_intent``.
_repost_intent: set[tuple[int, str]] = set()


def begin_repost_intent(user_id: int, session_id: str) -> None:
    """Mark (user, session) as repost-in-progress so concurrent
    claude events buffer instead of spawning their own card.

    Idempotent: re-marking a still-set pair is a no-op. Call
    ``end_repost_intent`` AFTER ``repost_card`` (success or failure)
    so the buffer drains. The buffer is the spawn-race fix's safety
    net — even if ``repost_card`` itself fails, ``end_repost_intent``
    lets normal rendering resume on the next event.
    """
    _repost_intent.add((user_id, session_id))


def end_repost_intent(user_id: int, session_id: str) -> None:
    """Clear the repost-in-progress flag set by ``begin_repost_intent``.

    Safe to call when no flag is set.
    """
    _repost_intent.discard((user_id, session_id))


def reset_card(user_id: int, session_id: str) -> None:
    """Drop the cached card so the next event creates a fresh message."""
    _cards.pop((user_id, session_id), None)


def _recover_from_false_stall(state: CardState) -> None:
    """Wipe the live-card binding after a false-positive stall_finalize.

    Set when a genuine assistant turn lands AFTER
    ``maybe_finalize_stalled`` armed ``state.stall_finalized``. Clears
    msg_id / events / pagination so the next render path goes through
    ``_send_card`` (fresh message below the stalled stub) rather than
    ``_edit_card`` (silent edit of the now-finalized card). The stalled
    stub stays in chat history with its STALL_NOTE — we don't rewrite
    it; the recovery message appears as a fresh card with
    ``is_continuation=True`` so the header carries the ``…continued``
    marker.
    """
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.msg_id = None
    state.events = []
    state.current_page_idx = None
    state.is_continuation = True
    state.last_rendered = ""
    state.seed_attempted = False
    state.seed_mtime = -1.0
    state.stall_finalized = False


async def cancel_pending_card_edits(timeout: float = 2.0) -> None:
    """Cancel + drain every deferred ``_edit_card`` task across all cards.

    Called from ``post_shutdown`` so we don't leave ``_deferred_edit``
    tasks in the "pending" state when the event loop closes — asyncio
    logs ``Task was destroyed but it is pending!`` for each one, and
    any in-flight Telegram edit can race with the final state save.
    """
    tasks: list[asyncio.Task[None]] = []
    for state in _cards.values():
        t = state.pending_edit
        if t is not None and not t.done():
            t.cancel()
            tasks.append(t)
        state.pending_edit = None
    if not tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            "card-edit shutdown drain timed out after %ss with %d tasks pending",
            timeout,
            sum(1 for t in tasks if not t.done()),
        )


async def close_card_view(bot: Bot, user_id: int, session_id: str) -> None:
    """Release the live card slot so the next event creates a fresh
    message instead of editing the old carrier.

    Used by the Shot button (Task #51): the screenshot photo replaces
    the live card visually, and when the user comes back from the
    screenshot we want a NEW card message to appear (replacement of
    one message by another), not an in-place edit of a now-stale
    carrier far up the chat.

    Steps:
      - Cancel any pending edit on the old carrier.
      - **Delete** the old carrier message so the chat reads as a
        clean replacement (per #52 follow-up — stripping the keyboard
        was confusing, the orphaned message read like a frozen card).
      - Drop ``msg_id`` so the next claude event / Shot Back spawns a
        fresh card.
      - Leave ``in_menu_view=True`` so events buffer until the user
        actually navigates back (the Shot Back handler clears it).
    """
    state = _cards.get((user_id, session_id))
    if state is None:
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    old_msg_id = state.msg_id
    state.msg_id = None
    state.is_photo_msg = False
    state.last_rendered = ""
    state.last_pane_hash = ""
    state.last_photo_edit_ts = 0.0
    state.in_menu_view = True
    if old_msg_id is not None:
        try:
            await bot.delete_message(chat_id=user_id, message_id=old_msg_id)
        except Exception as e:
            logger.debug(
                "close_card_view: delete old msg failed msg_id=%s: %s",
                old_msg_id,
                e,
            )
    logger.info(
        "card_close user=%d sess=%s old_msg_id=%s",
        user_id,
        session_id,
        old_msg_id,
        extra={
            "event": "card_close",
            "user_id": user_id,
            "session_id": session_id,
            "old_msg_id": old_msg_id,
        },
    )


def set_card_context_pct(user_id: int, session_id: str, pct: int) -> None:
    """Stash the latest context-window fill percentage for this session's
    live card. Read by ``_render_card`` to paint a ``context: N%`` line
    above the bg-status panel. No-op when no state exists yet.
    """
    state = _cards.setdefault((user_id, session_id), CardState())
    state.context_pct = pct


def mark_card_paused(user_id: int, session_id: str) -> None:
    """Force a card to ``in_menu_view=True``, creating empty state if
    none exists. Differs from :func:`pause_card_view` which silently
    no-ops on a missing state — needed for the Shot → switcher path
    where the user pivots onto a session whose card was never seeded.
    """
    _cards.setdefault((user_id, session_id), CardState()).in_menu_view = True


def pause_card_view(user_id: int, session_id: str) -> None:
    """Mark the live card paused so session updates buffer instead of
    rendering. Called when the user opens a Menu / sub-screen on the
    card's message — otherwise a stream of tool calls would overwrite
    whatever they're looking at."""
    state = _cards.get((user_id, session_id))
    if state is None:
        logger.info(
            "card_pause skip user=%d sess=%s reason=no_state",
            user_id,
            session_id,
            extra={
                "event": "card_pause_skip",
                "user_id": user_id,
                "session_id": session_id,
                "reason": "no_state",
            },
        )
        return
    state.in_menu_view = True
    logger.info(
        "card_pause user=%d sess=%s msg_id=%s lines=%d",
        user_id,
        session_id,
        state.msg_id,
        len(state.events),
        extra={
            "event": "card_pause",
            "user_id": user_id,
            "session_id": session_id,
            "msg_id": state.msg_id,
            "lines": len(state.events),
        },
    )


def transfer_card_to_carrier(
    user_id: int,
    from_session_id: str | None,
    to_session_id: str,
    target_message_id: int,
) -> None:
    """Hand off ownership of ``target_message_id`` from one session's
    live card to another's. Called when the switcher flips active.

    Effect:
      - FROM session is paused (``in_menu_view=True``) so its events
        buffer silently in ``state.events`` instead of editing the
        carrier (which now belongs to the TO session). No new chat
        message lands until the user switches back or types text.
      - TO session claims the carrier (``msg_id=target_message_id``)
        and its pause is released, so the next event for it renders
        on the carrier — overlaying the preview that the callback
        just painted.

    No-op when ``from_session_id == to_session_id`` (user tapped the
    already-active session). The previous live-card behaviour — where
    A's lingering ``msg_id`` clobbered B's preview every time A emitted
    a tool call — falls out naturally because A is now paused.
    """
    if from_session_id == to_session_id:
        logger.info(
            "card_transfer skip user=%d sess=%s reason=same_session",
            user_id,
            to_session_id,
            extra={
                "event": "card_transfer_skip",
                "user_id": user_id,
                "session_id": to_session_id,
                "reason": "same_session",
            },
        )
        return
    from_msg_id_was: int | None = None
    if from_session_id:
        from_state = _cards.get((user_id, from_session_id))
        if from_state is not None:
            from_msg_id_was = from_state.msg_id
            if (
                from_state.pending_edit is not None
                and not from_state.pending_edit.done()
            ):
                from_state.pending_edit.cancel()
            from_state.pending_edit = None
            from_state.in_menu_view = True
    to_state = _cards.setdefault((user_id, to_session_id), CardState())
    to_msg_id_was = to_state.msg_id
    if to_state.pending_edit is not None and not to_state.pending_edit.done():
        to_state.pending_edit.cancel()
    to_state.pending_edit = None
    to_state.msg_id = target_message_id
    session_manager.set_card_msg(user_id, target_message_id)
    # Pause the TO card across the switch window. The caller (CB_SW_USE)
    # will paint history on this message_id next, and then call
    # ``release_card_message`` which clears both ``msg_id`` and
    # ``in_menu_view``. If we left ``in_menu_view=False`` here, any bg
    # event arriving in the ~150 ms parse + edit window would trigger
    # ``refresh_panel`` — that path sees
    # ``msg_id=carrier`` + ``in_menu_view=False`` and rerenders the
    # live-card body over the carrier, clobbering the history paint
    # we're racing to land. Symptom: user sees "header + bg panel"
    # instead of transcript after a switch.
    to_state.in_menu_view = True
    logger.info(
        "card_transfer user=%d from=%s (from_msg=%s) to=%s (was_msg=%s) carrier=%s",
        user_id,
        from_session_id or "-",
        from_msg_id_was,
        to_session_id,
        to_msg_id_was,
        target_message_id,
        extra={
            "event": "card_transfer",
            "user_id": user_id,
            "from_session_id": from_session_id,
            "from_msg_id_was": from_msg_id_was,
            "to_session_id": to_session_id,
            "to_msg_id_was": to_msg_id_was,
            "carrier_msg_id": target_message_id,
        },
    )


def detach_paused_cards_at_message(user_id: int, message_id: int) -> None:
    """Release card state bound to ``message_id`` when the carrier has
    been repurposed for a different flow.

    The pause→resume design assumes the user eventually returns to the
    live card via ``resume_card_view`` (typing text, etc.). But when
    the carrier message gets hijacked for a different session — e.g.
    user navigates ``Menu → ＋ new`` and confirms a directory, the new
    session's "Created" status now owns the message — the OLD session's
    pause never gets released and its events buffer forever. Worse,
    ``state.msg_id`` still points at a message that's no longer its
    card, so a later edit would clobber whatever's there.

    This helper resets ``msg_id`` (so the next event opens a fresh
    card) and clears the pause flags for every card on this user that
    happened to be paused on the now-stolen message.
    """
    detached: list[str] = []
    for (uid, sid), state in list(_cards.items()):
        if uid != user_id or state.msg_id != message_id:
            continue
        if state.pending_edit is not None and not state.pending_edit.done():
            state.pending_edit.cancel()
        state.pending_edit = None
        state.msg_id = None
        state.in_menu_view = False
        # Mark continuation so the next card visually flags carry-over
        # (``…continued`` in the header).
        state.is_continuation = True
        detached.append(sid)
    if detached:
        logger.info(
            "card_detach user=%d msg=%s sessions=%s",
            user_id,
            message_id,
            detached,
            extra={
                "event": "card_detach",
                "user_id": user_id,
                "msg_id": message_id,
                "sessions": detached,
            },
        )


def release_card_message(user_id: int, session_id: str) -> None:
    """Drop the live-card binding to its current Telegram message_id
    without touching the message itself.

    Called from the switcher-tap handler right after history is painted
    on the carrier: the carrier now holds a frozen transcript view, and
    the TO session's live card must NOT keep editing it. With ``msg_id``
    cleared, the next claude event opens a fresh card below (carrying
    the bg-status panel and prior-context seed); the history carrier
    stays put and remains paginable.

    Buffered ``lines`` are also wiped — they were destined for the
    overwritten card; the fresh card starts empty on its next event.
    """
    state = _cards.get((user_id, session_id))
    if state is None:
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.msg_id = None
    state.in_menu_view = False
    state.events = []
    state.last_rendered = ""
    state.is_continuation = True
    # A6: this is a non-destructive carrier hand-off — the session keeps
    # running with a full transcript. Allow the next event's fresh card
    # to re-seed so its footer page counter reflects the real recent
    # turn-history instead of collapsing to ``1/1``.
    state.seed_attempted = False
    state.seed_mtime = -1.0
    logger.info(
        "card_release user=%d sess=%s",
        user_id,
        session_id,
        extra={
            "event": "card_release",
            "user_id": user_id,
            "session_id": session_id,
        },
    )


async def resume_card_view(bot: Bot, user_id: int, sess: Session) -> None:
    """Drop the menu-pause so future events render again, and re-paint
    the carrier with the buffered events.

    CRITICAL: clears ``in_menu_view`` UNCONDITIONALLY when the state
    exists — even when ``msg_id`` was lost (carrier stale / deleted /
    not yet created). Earlier this returned early without clearing
    the pause, leaving the card stuck in ``must_buffer=True`` forever
    (symptom: chronic ``card_update buffered`` log, body never updates
    even though claude is producing events). When ``msg_id`` is None
    we still clear the flag; the next claude event spawns a fresh
    card via ``_send_card`` because ``state.msg_id is None``.
    """
    # ``setdefault`` so a session with no card-state yet (just-switched
    # bg session via Shot's switcher) still lands on a visible surface.
    # Without this, resume_card_view silently bailed and Back left the
    # user staring at empty chat.
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.in_menu_view = False
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None

    async def _spawn_fresh() -> None:
        await _ensure_seeded(user_id, sess, state)
        fresh_text = _render_card(sess, state, user_id=user_id)
        fresh_kb = build_footer_keyboard(user_id, screen="main", is_busy=True)
        await _send_card(
            bot, user_id, sess, state, text=fresh_text, reply_markup=fresh_kb
        )

    # Spawn-serialization (Task #50): hold the per-session lock across
    # the msg_id check + send/edit. Otherwise a claude event arriving
    # during ``_ensure_seeded`` / ``_send_card`` can race and produce a
    # duplicate card via ``update_session_card``.
    async with _card_lock(user_id, sess.id):
        if state.msg_id is None:
            # No carrier — spawn a fresh card now so the user lands on a
            # visible surface immediately (used by Shot → Back after #51's
            # ``close_card_view`` drops msg_id). Previously we waited for
            # the next claude event; on quiet sessions that left the user
            # staring at empty chat.
            await _spawn_fresh()
            return
        text = _render_card(sess, state, user_id=user_id)
        keyboard = build_footer_keyboard(user_id, screen="main", is_busy=True)
        if await _edit_card(bot, user_id, state, text=text, reply_markup=keyboard):
            state.last_rendered = text
            state.last_edit_ts = time.monotonic()
            return
        # ``_edit_card`` returned False — the carrier was lost (stale msg,
        # already-deleted, or bot can't edit it) and ``_edit_card`` has
        # already reset msg_id internally. Spawn a fresh card so the user
        # still lands on a visible live surface.
        await _spawn_fresh()


async def paint_card_on_carrier(
    bot: Bot,
    user_id: int,
    sess: Session,
    carrier_msg_id: int,
) -> None:
    """Claim ``carrier_msg_id`` as ``sess``'s live card and paint it.

    Used by Menu → Sessions: the carrier is the menu message the user just
    tapped, and we want it to become the live card (one unified surface
    instead of a separate list rendering). The previous ``state.msg_id``
    is left as a frozen artifact in chat — the next claude event uses
    the new carrier.
    """
    state = _cards.setdefault((user_id, sess.id), CardState())
    # Menu → Sessions on a fresh post-restart state: seed history first
    # so the user lands on a card with their conversation, not 1/1.
    await _ensure_seeded(user_id, sess, state)
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.msg_id = carrier_msg_id
    state.in_menu_view = False
    state.last_rendered = ""
    _register_msg(user_id, carrier_msg_id, sess.id)
    session_manager.set_card_msg(user_id, carrier_msg_id)
    text = _render_card(sess, state, user_id=user_id)
    keyboard = build_footer_keyboard(
        user_id, screen="main", is_busy=_card_is_busy(state)
    )
    if await _edit_card(bot, user_id, state, text=text, reply_markup=keyboard):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
        # Migrate the switcher pointer onto the new carrier so previous
        # switcher rows in chat stop being the canonical surface.
        prev = session_manager.get_last_switcher_msg(user_id)
        if prev and prev != carrier_msg_id:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=user_id, message_id=prev, reply_markup=None
                )
            except Exception:
                pass
        session_manager.set_last_switcher_msg(user_id, carrier_msg_id)


async def restore_card(bot: Bot, user_id: int, sess: Session, card_msg_id: int) -> bool:
    """Repaint a persisted live card in place after a bot restart.

    ``_cards`` is in-memory only, so a restart loses every live card's
    ``CardState`` and the chat is left with a frozen, orphaned card
    message. The card's ``message_id`` is persisted per active session
    (``session_manager.card_msg_id``); on startup we rebuild a fresh
    ``CardState``, seed the recent transcript from JSONL, and edit the
    original message in place so the live card resumes on the same
    message instead of a new one appearing on the next event.

    Returns True if the in-place edit landed. On failure (message
    deleted by the user, edit rejected) the stale pointer is cleared so
    the next claude event spawns a fresh card normally.
    """
    existing = _cards.get((user_id, sess.id))
    if existing is not None and existing.msg_id is not None:
        # A claude event already raced ahead and established a live card
        # for this session — leave it alone rather than fight it.
        return True
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = card_msg_id
    state.last_rendered = ""
    await _ensure_seeded(user_id, sess, state)
    _register_msg(user_id, card_msg_id, sess.id)
    text = _render_card(sess, state, user_id=user_id)
    keyboard = build_footer_keyboard(
        user_id, screen="main", is_busy=_card_is_busy(state)
    )
    if await _edit_card(bot, user_id, state, text=text, reply_markup=keyboard):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
        return True
    # The message is gone — drop both the cached state and the persisted
    # pointer so the next event creates a fresh card cleanly.
    _cards.pop((user_id, sess.id), None)
    session_manager.clear_card_msg(user_id)
    return False


async def clear_card(bot: Bot, user_id: int, sess: Session) -> None:
    """Wipe the live card's body in response to a user-driven /clear.

    Edits the existing message to a header-only "(cleared)" snapshot
    so the user sees the previous tool log disappear, then drops the
    cached state so the next claude event spawns a fresh card.
    No-op when there is no live card.
    """
    state = _cards.get((user_id, sess.id))
    if state is None or state.msg_id is None:
        reset_card(user_id, sess.id)
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.events = []
    state.last_rendered = ""
    text = _render_card(sess, state, footer="(cleared)", user_id=user_id)
    cleared_kb = build_footer_keyboard(user_id, screen="main", is_busy=False)
    await _edit_card(bot, user_id, state, text=text, reply_markup=cleared_kb)
    reset_card(user_id, sess.id)


async def _send_card(
    bot: Bot,
    user_id: int,
    sess: Session,
    state: CardState,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send a brand-new card message and remember it as the live card.

    ``reply_markup`` overrides the default footer keyboard. Used by
    ``finalize_task`` to attach the idle-state Kill row to a completed
    result instead of the busy-state Stop row.
    """
    if reply_markup is None:
        # Default: a fresh card is being sent because a turn is in
        # flight (update_session_card, repost_card, continuation
        # overflow). ``_card_is_busy`` keys off ``state.msg_id`` which
        # is still None at this point — the right signal here is
        # "we're sending a card", which by definition means Stop is
        # the user's intent. ``finalize_task`` overrides ``reply_markup``
        # explicitly with the Kill keyboard when a turn completes.
        reply_markup = build_footer_keyboard(user_id, screen="main", is_busy=True)
    keyboard = reply_markup

    # Inline screenshots ON + we have a window_id: send photo+caption.
    sent = None
    if _inline_screens_enabled(user_id) and sess.window_id:
        from ..markdown_v2 import convert_markdown
        from .message_sender import PARSE_MODE, strip_sentinels

        png, pane_hash = await _capture_pane_png(sess.window_id)
        if png is not None:
            import io as _io

            caption = convert_markdown(text)
            try:
                sent = await bot.send_photo(
                    chat_id=user_id,
                    photo=_io.BytesIO(png),
                    caption=caption,
                    parse_mode=PARSE_MODE,
                    reply_markup=keyboard,
                    disable_notification=True,
                )
            except RetryAfter:
                raise
            except Exception as e:
                logger.debug("photo send failed, retry plain caption: %s", e)
                try:
                    sent = await bot.send_photo(
                        chat_id=user_id,
                        photo=_io.BytesIO(png),
                        caption=strip_sentinels(text),
                        reply_markup=keyboard,
                        disable_notification=True,
                    )
                except Exception as e2:
                    logger.debug("photo send plain fallback failed: %s", e2)
            if sent is not None:
                state.is_photo_msg = True
                state.last_pane_hash = pane_hash
                state.last_photo_edit_ts = time.monotonic()

    # Text-mode card OR photo path failed → text fallback.
    if sent is None:
        from .message_sender import send_with_fallback

        try:
            sent = await send_with_fallback(
                bot,
                user_id,
                text,
                reply_markup=keyboard,
                disable_notification=True,
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.debug("card send failed: %s", e)
            return
        if sent is None:
            return
        state.is_photo_msg = False
    # Strip the previous switcher (if any), then remember this one as the
    # carrier of the live switcher.
    prev = session_manager.get_last_switcher_msg(user_id)
    if prev and prev != sent.message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=user_id, message_id=prev, reply_markup=None
            )
        except Exception:
            pass
    if keyboard is not None:
        session_manager.set_last_switcher_msg(user_id, sent.message_id)
    state.msg_id = sent.message_id
    state.last_rendered = text
    _register_msg(user_id, sent.message_id, sess.id)
    session_manager.set_card_msg(user_id, sent.message_id)


async def _edit_card(
    bot: Bot,
    user_id: int,
    state: CardState,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit the live card. Returns False if the edit failed permanently.

    Always sends a keyboard along with the text — relying on Telegram's
    "preserve keyboard when reply_markup is omitted" semantics turned out
    flaky (the buttons flickered between edits). Caller may pass an
    explicit `reply_markup`; otherwise we rebuild from current busy state.
    """
    if state.msg_id is None:
        return False
    # User is currently looking at a Menu / sub-screen on this card's
    # message. Don't repaint — would clobber whatever they're navigating.
    # State.lines keeps accumulating; resume_card_view will catch up.
    if state.in_menu_view:
        return True
    if reply_markup is None:
        reply_markup = build_footer_keyboard(
            user_id, screen="main", is_busy=_card_is_busy(state)
        )
    from ..markdown_v2 import convert_markdown
    from .message_sender import (
        NO_LINK_PREVIEW,
        PARSE_MODE,
        strip_sentinels,
        try_rich_edit,
    )

    # Photo-mode card: editMessageMedia when pane changed (≤1 per 3s),
    # else editMessageCaption to refresh just the text. Captions have no
    # rich-message equivalent, so this path stays MarkdownV2.
    if state.is_photo_msg:
        return await _edit_photo_card(
            bot,
            user_id,
            state,
            text=text,
            formatted=convert_markdown(text),
            reply_markup=reply_markup,
        )

    # Rich-first (Bot API 10.1): keeps the card's native rendering (GFM
    # tables, headings, <details>) consistent with the rich _send_card
    # path — otherwise the first edit would visibly downgrade the card
    # to MarkdownV2. On failure (rich off, API error, lost carrier) fall
    # through to the MarkdownV2 pipeline below, which also owns the
    # lost-carrier detection.
    if await try_rich_edit(bot, user_id, state.msg_id, text, reply_markup=reply_markup):
        return True

    formatted = convert_markdown(text)

    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state.msg_id,
            text=formatted,
            parse_mode=PARSE_MODE,
            reply_markup=reply_markup,
            link_preview_options=NO_LINK_PREVIEW,
        )
        return True
    except BadRequest as e:
        err = str(e)
        if "Message is not modified" in err:
            return True
        if (
            "Message to edit not found" in err
            or "message can't be edited" in err.lower()
            or "MESSAGE_ID_INVALID" in err
        ):
            # Carrier is genuinely gone — reset msg_id so the next event
            # opens a fresh card.
            logger.info("card edit lost-carrier msg_id=%s err=%s", state.msg_id, err)
            state.msg_id = None
            return False
        # Parse error / can't render — fall back to stripped plain text
        # on the SAME carrier. Keep the card alive.
        logger.warning("card edit MarkdownV2 failed msg=%s err=%s", state.msg_id, err)
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=state.msg_id,
                text=strip_sentinels(text),
                reply_markup=reply_markup,
                link_preview_options=NO_LINK_PREVIEW,
            )
            return True
        except BadRequest as e2:
            err2 = str(e2)
            if "Message is not modified" in err2:
                return True
            logger.warning(
                "card edit plain fallback failed msg=%s err=%s", state.msg_id, err2
            )
        except RetryAfter:
            raise
        except Exception as e2:
            logger.warning(
                "card edit plain fallback exc msg=%s err=%s", state.msg_id, e2
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.warning("card edit failed (other): %s", e)
    return False


_PHOTO_EDIT_MIN_INTERVAL = 2.5  # seconds — per-session throttle on editMessageMedia


async def _edit_photo_card(
    bot: Bot,
    user_id: int,
    state: CardState,
    *,
    text: str,
    formatted: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> bool:
    """Edit a photo+caption card msg.

    Refresh strategy:
    * Pane unchanged since last edit → editMessageCaption only.
    * Pane changed AND ≥3s since last photo edit → editMessageMedia
      with new photo + new caption + keyboard.
    * Pane changed but throttled → editMessageCaption only. Next render
      after the throttle window will pick up the freshest pane.
    """
    import io as _io

    from telegram import InputMediaPhoto

    from ..markdown_v2 import convert_markdown
    from .message_sender import PARSE_MODE, strip_sentinels

    # Resolve session from msg_id lookup (we don't have it here directly).
    # Find by reverse mapping (user_id, msg_id) → session_id.
    sess_id = lookup_session_for_message(user_id, state.msg_id or 0)
    sess = session_manager.get_session(sess_id) if sess_id else None
    window_id = sess.window_id if sess is not None else ""

    pane_changed = False
    pane_png: bytes | None = None
    pane_hash = state.last_pane_hash
    elapsed = time.monotonic() - state.last_photo_edit_ts
    if window_id and elapsed >= _PHOTO_EDIT_MIN_INTERVAL:
        png, h = await _capture_pane_png(window_id)
        if png is not None and h:
            if h != state.last_pane_hash:
                pane_changed = True
                pane_png = png
                pane_hash = h

    try:
        if pane_changed and pane_png is not None:
            media = InputMediaPhoto(
                media=_io.BytesIO(pane_png),
                caption=convert_markdown(text),
                parse_mode=PARSE_MODE,
            )
            await bot.edit_message_media(
                chat_id=user_id,
                message_id=state.msg_id,
                media=media,
                reply_markup=reply_markup,
            )
            state.last_pane_hash = pane_hash
            state.last_photo_edit_ts = time.monotonic()
            return True
        # Pane unchanged or throttled — caption-only refresh.
        await bot.edit_message_caption(
            chat_id=user_id,
            message_id=state.msg_id,
            caption=formatted,
            parse_mode=PARSE_MODE,
            reply_markup=reply_markup,
        )
        return True
    except BadRequest as e:
        err = str(e)
        if "Message is not modified" in err:
            return True
        if (
            "Message to edit not found" in err
            or "message can't be edited" in err.lower()
            or "MESSAGE_ID_INVALID" in err
        ):
            logger.info("photo card edit lost-carrier msg=%s err=%s", state.msg_id, err)
            state.msg_id = None
            return False
        logger.warning(
            "photo card edit MarkdownV2 failed msg=%s err=%s", state.msg_id, err
        )
        # Plain-text caption fallback.
        try:
            await bot.edit_message_caption(
                chat_id=user_id,
                message_id=state.msg_id,
                caption=strip_sentinels(text),
                reply_markup=reply_markup,
            )
            return True
        except Exception as e2:
            logger.warning(
                "photo card plain fallback failed msg=%s err=%s", state.msg_id, e2
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.warning("photo card edit failed (other): %s", e)
    return False


async def _deferred_edit(
    bot: Bot, user_id: int, sess: Session, state: CardState, delay: float
) -> None:
    """Sleep `delay` then render the latest card state and edit once.

    The deferred task always picks up the latest `state.events`, so multiple
    events arriving during the sleep collapse into a single edit.
    """
    try:
        await asyncio.sleep(delay)
        # Stale guard: card may have been reset (finalize_task) while we slept.
        if state.msg_id is None:
            return
        text = _render_card(sess, state, user_id=user_id)
        if text == state.last_rendered:
            return
        if await _edit_card(bot, user_id, state, text=text):
            state.last_rendered = text
            state.last_edit_ts = time.monotonic()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.debug("deferred card edit failed: %s", e)
    finally:
        state.pending_edit = None


async def update_session_card(
    bot: Bot, user_id: int, sess: Session, msg: NewMessage
) -> None:
    """Append `msg` to the session's live card, or open a new one if needed.

    Triggers a fresh card on long pause and on hard-limit overflow.
    """
    # Fire a (throttled) background prewarm of the pages cache so the
    # live-card's ◀ Older N/N counter has a value to render on the
    # next event. The first event after session start may still paint
    # without the counter — the background task lands within a second.
    if sess.window_id:
        from .history import kick_prewarm

        kick_prewarm(sess.window_id)

    state = get_card_state(user_id, sess)
    # First event after a bot restart: pull JSONL history into events
    # so the card shows context, not a single 1/1 page.
    await _ensure_seeded(user_id, sess, state)

    # Should we buffer this event instead of rendering it now? Reasons:
    # - the user is on a Menu / sub-screen (state.in_menu_view set by
    #   pause_card_view or transfer_card_to_carrier);
    # - the session isn't the user's currently-active one (live check —
    #   bg sessions must stay silent in chat).
    # The check is in ``_should_buffer`` so future buffering reasons
    # converge on the same predicate. Previously the bg branch was
    # implemented by force-setting ``state.in_menu_view = True`` here;
    # the flag was sticky and outlived the bg phase, leaving the card
    # permanently paused — until a typed message woke
    # ``resume_card_view``. Computing the bg check live fixes that.
    must_buffer = _should_buffer(user_id, sess.id, state)

    msg_id_in = state.msg_id
    in_menu_view_in = state.in_menu_view

    new_event = _build_event(msg)
    # tool_result: fold into the matching tool_use Event in place.
    # If no match (race / restart), append the placeholder as a row.
    replaced = False
    if msg.content_type == "tool_result":
        replaced = _apply_tool_result(state, new_event)

    # Buffer-only path: user is on a menu/sub-screen OR session is bg.
    # Buffer the event into ``state.events`` so resume / next switcher
    # tap can catch up; do NOT trigger stale-card resets, overflow
    # continuations, or any rendering.
    if must_buffer:
        if not replaced and not _duplicate_of_seeded(state.events, new_event):
            # Same dedup guard as the live path: a prior seed (line ~1371)
            # may already hold this turn; don't buffer a second copy.
            state.events.append(new_event)
        state.last_event_ts = time.time()
        logger.info(
            "card_update buffered sess=%s msg_id=%s ctype=%s lines=%d",
            sess.id,
            msg_id_in,
            msg.content_type,
            len(state.events),
            extra={
                "event": "card_update_buffered",
                "user_id": user_id,
                "session_id": sess.id,
                "msg_id": msg_id_in,
                "content_type": msg.content_type,
                "lines": len(state.events),
                "in_menu_view": in_menu_view_in,
            },
        )
        return

    # Spawn-serialization (Task #50): hold the per-session lock from
    # the stale-check through the actual send/edit. Otherwise two
    # concurrent ``update_session_card`` calls (or one
    # ``update_session_card`` racing with ``resume_card_view`` /
    # ``repost_card`` / ``finalize_task``) can both see ``msg_id is
    # None`` and both spawn — produces "2 messages in wrong order".
    async with _card_lock(user_id, sess.id):
        return await _update_session_card_locked(
            bot, user_id, sess, msg, state, new_event, replaced
        )


async def _update_session_card_locked(
    bot: Bot,
    user_id: int,
    sess: Session,
    msg: NewMessage,
    state: CardState,
    new_event: Event,
    replaced: bool,
) -> None:
    # Recover from a prior false-positive stall_finalize. Wipe the card
    # binding so this real assistant turn lands on a fresh message
    # below the stalled stub instead of being silently edited into it.
    if state.stall_finalized:
        _recover_from_false_stall(state)
        await _ensure_seeded(user_id, sess, state)
    # Trigger: long pause → fresh card.
    if _is_stale(state):
        state.msg_id = None
        state.events = []
        state.current_page_idx = None
        state.is_continuation = True
        state.last_rendered = ""
        # A6: re-seed the recent transcript so the fresh card lands with
        # its full turn-history. Without this the card rebuilds one event
        # at a time and the footer page counter shows ``1/1`` until a
        # second turn completes — even though the transcript is long.
        state.seed_attempted = False
        state.seed_mtime = -1.0
        await _ensure_seeded(user_id, sess, state)

    if not replaced and not _duplicate_of_seeded(state.events, new_event):
        # Dedup guard: if the stale-branch re-seed above (or an earlier
        # release_card_message wipe) already pulled this turn in from
        # JSONL, don't append it a second time — otherwise the user's own
        # message renders twice in the card body.
        state.events.append(new_event)
        # User-action-anchor: when on the latest page, every new event
        # keeps the user there. Done as None (=stick-to-latest) so the
        # render layer picks the latest page automatically.
        # Page idx is recalibrated by paginate-aware callbacks.

    # Cap event log to avoid unbounded memory; FIFO evicts oldest.
    if len(state.events) > CARD_MAX_EVENTS:
        del state.events[: len(state.events) - CARD_MAX_EVENTS]

    state.last_event_ts = time.time()

    # Pagination handles size: the latest page is always within
    # CARD_HARD_LIMIT chars (paginate splits before the boundary).
    # No continuation-card path.

    text = _render_card(sess, state, user_id=user_id)

    if state.msg_id is None:
        await _send_card(bot, user_id, sess, state, text=text)
        state.last_edit_ts = time.monotonic()
        logger.info(
            "card_update sent sess=%s msg_id=%s ctype=%s lines=%d",
            sess.id,
            state.msg_id,
            msg.content_type,
            len(state.events),
            extra={
                "event": "card_update_sent",
                "user_id": user_id,
                "session_id": sess.id,
                "msg_id": state.msg_id,
                "content_type": msg.content_type,
                "lines": len(state.events),
            },
        )
        return

    # Coalesce edits — at most one editMessageText per live_lag seconds.
    # User setting takes precedence over the env-var default.
    user_lag = session_manager.get_user_settings(user_id).get("live_lag")
    if user_lag is None:
        user_lag = config.card_edit_lag
    lag = max(0.0, float(user_lag))
    elapsed = time.monotonic() - state.last_edit_ts if state.last_edit_ts else lag
    if lag <= 0 or elapsed >= lag:
        edited = await _edit_card(bot, user_id, state, text=text)
        if edited:
            state.last_rendered = text
            state.last_edit_ts = time.monotonic()
            logger.info(
                "card_update edit sess=%s msg_id=%s ctype=%s lines=%d",
                sess.id,
                state.msg_id,
                msg.content_type,
                len(state.events),
                extra={
                    "event": "card_update_edited",
                    "user_id": user_id,
                    "session_id": sess.id,
                    "msg_id": state.msg_id,
                    "content_type": msg.content_type,
                    "lines": len(state.events),
                },
            )
        else:
            # Edit failed AND we couldn't recover — DO NOT fall back to
            # _send_card here. Sending a new message produces duplicate
            # cards in chat (this was the "2 messages in a row" bug:
            # Message_too_long → fallback send → stale card stays + new
            # appears). Caller's next event retries the edit; if the
            # carrier message is truly gone (deleted, too old), the
            # ``Message to edit not found`` branch in _edit_card resets
            # msg_id and a fresh card spawns on the next event.
            state.last_edit_ts = time.monotonic()
            logger.warning(
                "card_update edit_failed sess=%s msg_id=%s — keeping "
                "stale card; new render will retry on next event",
                sess.id,
                state.msg_id,
            )
        return

    # Inside the coalescing window: ensure exactly one deferred edit is queued.
    if state.pending_edit is None or state.pending_edit.done():
        delay = max(0.05, lag - elapsed)
        state.pending_edit = asyncio.create_task(
            _deferred_edit(bot, user_id, sess, state, delay)
        )


async def finalize_task(bot: Bot, user_id: int, sess: Session, final_text: str) -> None:
    """Append the final assistant answer to the current live card.

    Appends a ``final_text`` Event with ``is_page_break=True`` so the
    new answer anchors the top of the latest page; everything before
    it (tool log, thinking, mid-stream text) lives on the previous page.
    The user lands on the new latest page by default. Long answers
    that exceed Telegram's 4096-char limit are sub-paginated by
    ``paginate_events``.
    """
    state = get_card_state(user_id, sess)
    # Recover from a prior false-positive stall_finalize: wipe the card
    # binding so this real answer spawns a fresh card below the stalled
    # stub. Must run before ``_ensure_seeded`` so the seed targets the
    # cleared events list. NOT triggered by ``maybe_finalize_stalled``'s
    # own call into ``finalize_task`` — the flag is set only AFTER that
    # path returns.
    if state.stall_finalized:
        _recover_from_false_stall(state)
    # First event after a bot restart: seed JSONL history before
    # appending the final answer so the user sees their context.
    await _ensure_seeded(user_id, sess, state)

    # Bg-session silence + menu-pause buffering. Same predicate as
    # update_session_card — see ``_should_buffer`` for the rationale.
    must_buffer = _should_buffer(user_id, sess.id, state)

    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None

    cleaned = (final_text or "").strip()
    if not cleaned:
        # No final text from Claude (e.g. /clear with nothing else).
        # Drop the card; no push — the previous "completion push"
        # behaviour was removed when the result moved into the card body.
        if must_buffer:
            return
        reset_card(user_id, sess.id)
        return

    formatted = split_overflow(cleaned)
    cleaned = formatted.text
    attachments = formatted.attachments

    # Final answer = ONE OR MORE is_page_break Events: each chunk
    # anchors a new page. ``_chunk_final_text`` keeps every chunk under
    # ``card_page_lines`` user-setting (in LINES) so the rendered page
    # respects what the user picked. Smart boundaries (paragraph / line
    # / sentence / word) prevent mid-content breaks. Default focus lands
    # on the FIRST chunk's page so the user reads the answer from the top.
    now = time.time()
    stripped_full = _strip_for_card(cleaned)
    chunks = _chunk_final_text(stripped_full, _resolve_line_budget(user_id))
    final_events = [
        Event(
            type="final_text",
            text=chunk,
            body=chunk,
            started_at=now,
            completed_at=now,
            is_page_break=True,
        )
        for chunk in chunks
    ]

    # Buffer-only path: user is on a Menu view OR session is bg.
    # Accumulate the answer Events into state. resume_card_view (next
    # typed message) / switcher tap will render the catch-up.
    # Attachments still go out (file delivery shouldn't wait on UI nav).
    if must_buffer:
        state.events.extend(final_events)
        state.last_event_ts = now
        if attachments:
            await _send_attachments(bot, user_id, attachments)
        return

    state.events.extend(final_events)
    if len(state.events) > CARD_MAX_EVENTS:
        del state.events[: len(state.events) - CARD_MAX_EVENTS]
    state.last_event_ts = now
    # Default focus: when the answer was split into N chunks, land on
    # the FIRST chunk's page so the user starts at the top. When there's
    # only one chunk, ``None`` = latest, which is that same page.
    if len(final_events) > 1:
        pages_after = paginate_events_for_card(state, user_id)
        # The first chunk's page is at index (len(pages) - len(chunks)).
        first_chunk_page = max(0, len(pages_after) - len(final_events))
        state.current_page_idx = first_chunk_page
    else:
        state.current_page_idx = None

    # Refresh the pages cache so the live-card's pagination counter
    # reflects the final transcript length on the finalised message.
    # This is the one place we await prewarm directly — finalize fires
    # once per task, so ~1 s of parsing is OK to pay for a correct
    # ◀ Older N/N counter on the artifact the user looks at most.
    if sess.window_id:
        try:
            from .history import prewarm_pages_cache

            await prewarm_pages_cache(sess.window_id)
        except Exception as e:
            logger.debug("finalize_task prewarm failed: %s", e)

    # Final answer → ``is_busy=False`` keyboard so the user sees Kill,
    # not Stop. State stays live (rolling card); the next turn's events
    # keep editing the SAME message — no reset_card, no pin.
    done_kb = build_footer_keyboard(user_id, screen="main", is_busy=False)

    text = _render_card(sess, state, user_id=user_id)
    # Lock the spawn/edit decision so a parallel ``update_session_card``
    # for the next turn can't see ``msg_id is None`` simultaneously and
    # spawn a second card (Task #50).
    async with _card_lock(user_id, sess.id):
        if state.msg_id is None:
            await _send_card(bot, user_id, sess, state, text=text, reply_markup=done_kb)
        elif await _edit_card(bot, user_id, state, text=text, reply_markup=done_kb):
            state.last_rendered = text
        state.last_edit_ts = time.monotonic()

    if attachments:
        await _send_attachments(bot, user_id, attachments)


async def _send_attachments(
    bot: Bot, user_id: int, attachments: list[Attachment]
) -> None:
    """Send extracted overflow content. ``kind="photo"`` table extracts
    are rasterised via ``screenshot.text_to_image`` so wide tables land
    as inline images rather than `.md` files; everything else (oversized
    code blocks) goes through ``send_document`` as before.
    """
    import io as _io

    from ..screenshot import text_to_image
    from .tg_format import pretty_pad_table

    for att in attachments:
        try:
            if att.kind == "photo":
                source = att.content.decode("utf-8", errors="replace")
                rendered = pretty_pad_table(source)
                png = await text_to_image(rendered, with_ansi=False)
                await bot.send_photo(
                    chat_id=user_id,
                    photo=_io.BytesIO(png),
                )
            else:
                await bot.send_document(
                    chat_id=user_id,
                    document=_io.BytesIO(att.content),
                    filename=att.filename,
                )
        except Exception as e:
            logger.debug("attachment %s send failed: %s", att.filename, e)


async def push_event(
    bot: Bot,
    user_id: int,
    sess: Session,
    *,
    text: str,
    is_error: bool = False,
) -> None:
    """Bg-session push — a bare one-line notification.

    Format is strictly ``<emoji> <name> <text>``: no markdown brackets,
    no inline keyboard, no switcher migration. Hijacking the active
    card's footer buttons (the previous behaviour) confused users —
    bg pushes are status pings, not navigation surfaces. Use the
    switcher on the active card to actually visit the session.
    """
    emoji = "🟥" if is_error else session_emoji(sess)
    name = sess.name or sess.id
    body = f"{emoji} {name} {text}"
    if len(body) > 3500:
        body = body[:3497] + "…"
    try:
        sent = await safe_send(bot, user_id, body)
    except Exception as e:
        logger.debug("push_event failed: %s", e)
        return
    # Register the msg→session map so a reply-quote to this push still
    # routes back to the originating session.
    if sent is not None:
        _register_msg(user_id, sent.message_id, sess.id)


def is_card_in_menu_view(user_id: int, session_id: str) -> bool:
    """True if the user is currently browsing a Menu / sub-screen on
    this session's card. Used by ``status_polling`` to gate the TYPING
    indicator — firing it while the user navigates menus is just noise.
    """
    state = _cards.get((user_id, session_id))
    return state is not None and state.in_menu_view


def is_card_finalized(user_id: int, session_id: str) -> bool:
    """True when the card's tail event is a terminal one (``final_text``
    or ``error``). Used by ``status_polling`` to suppress a stale pane
    spinner (e.g. ``Sautéed for 11m 16s · 1 shell still running``) that
    persists in scrollback after end-of-turn — without this check the
    typing indicator stays on forever waiting for the user-visible
    spinner string to scroll off.
    """
    state = _cards.get((user_id, session_id))
    if state is None or not state.events:
        return False
    return state.events[-1].type in ("final_text", "error")


def is_card_busy(user_id: int, session_id: str) -> bool:
    """True when the user's live card for ``session_id`` is currently in
    flight AND visible (msg_id set, finalize_task hasn't run, and the
    card is not paused for menu navigation). Used by the polling-based
    typing-indicator path — TYPING should fire while a turn is mid-
    stream during silent gaps between events, but NOT while the user
    is browsing the inline ⋯ Menu / sub-screens. While ``in_menu_view``
    the card buffers events without rendering to chat, so a "typing…"
    indicator there is just noise.
    """
    state = _cards.get((user_id, session_id))
    if state is None or state.in_menu_view:
        return False
    return _card_is_busy(state)


# ─── Stalled-session detection (bug A4) ───────────────────────────────
#
# When the upstream claude subprocess silently stalls or exits
# mid-iteration, the JSONL stops growing with renderable turns (it may
# still get ``last-prompt`` / ``ai-title`` metadata entries, which
# transcript_parser filters out — see transcript_parser.py:260). The
# session monitor therefore produces ZERO card updates and the live
# card freezes on its last "thinking"/tool_use frame with no signal to
# the user. ``maybe_finalize_stalled`` closes that gap: when an active
# card has sat with a non-terminal tail event AND the pane spinner has
# been idle (gone or frozen) for ``STALL_FINALIZE_AFTER_SECONDS``, it
# finalises the card with a clear note so the user knows the process
# may have stalled rather than the bot being broken.

# How long an active card may sit with a non-terminal tail event and an
# idle (non-busy) pane before we declare it stalled and finalise it.
# Deliberately generous: a genuinely-busy claude keeps the pane spinner
# *changing* (``Working… (17s)`` → ``(18s)`` → …) so ``pane_busy`` stays
# True and this never fires during long thinking / a slow tool. We only
# trip when the spinner is gone or frozen AND no new renderable event
# arrived for this long — i.e. the subprocess produced nothing.
#
# Two-tier threshold by tail event type. Tools legitimately run for
# minutes (slow Bash, CHYT, Map-Reduce, network) and Claude routinely
# spends a comparable amount of time reasoning after the last tool
# result before emitting the final assistant turn — both produce a
# silent JSONL tail of ``tool_use``. Pre-textual silence (``text`` /
# ``thinking`` tail) is rarer and more suspicious because Claude is
# mid-emit, so the original threshold still applies there.
STALL_FINALIZE_AFTER_SECONDS = 90.0
STALL_FINALIZE_TOOL_USE_SECONDS = 300.0

# Note appended to the card when a stall is detected. Card-body strings
# in this module are not localized (header / "context:" / "goal:" are
# all hard-coded English), so this note follows the same convention.
STALL_NOTE = (
    "⚠️ session went idle without a final reply — "
    "the Claude process may have stalled or exited."
)


async def maybe_finalize_stalled(
    bot: Bot,
    user_id: int,
    sess: Session,
    *,
    pane_busy: bool,
    interactive_waiting: bool,
    in_menu: bool,
    now: float | None = None,
) -> bool:
    """Finalise an ACTIVE session's frozen card when the subprocess stalled.

    Fires (returns True after finalising) ONLY when ALL hold:

      * a card exists for this (user, session) with at least one event;
      * the card is NOT already finalized (tail event is non-terminal —
        mid ``thinking`` / ``tool_use`` / ``text``);
      * the pane spinner is NOT busy (``pane_busy=False`` — gone or
        frozen, per ``_pane_status_is_changing``);
      * no interactive UI is waiting for the user (``interactive_waiting``
        — AskUserQuestion / ExitPlanMode / Permission / RestoreCheckpoint,
        or kb-mode) and the card is not in a Menu sub-screen
        (``in_menu``);
      * no new renderable event arrived for ``STALL_FINALIZE_AFTER_SECONDS``
        (measured from ``state.last_event_ts``).

    The last condition is what keeps this conservative: a long-thinking
    turn keeps the pane spinner changing, so ``pane_busy`` is True and we
    bail; a tool_use legitimately awaiting a slow result either keeps the
    spinner alive or lands its result well before the window elapses. We
    only trip when the spinner has died AND the transcript stopped
    growing — the exact fingerprint of a stalled / exited subprocess.

    Reuses ``finalize_task``: the stall note is appended as the turn's
    final answer, so the card flips to the finalized (Kill, not Stop)
    keyboard via the same path a normal completion takes.
    """
    if pane_busy or interactive_waiting or in_menu:
        return False
    state = _cards.get((user_id, sess.id))
    if state is None or state.msg_id is None or not state.events:
        return False
    if state.in_menu_view or state.in_kb_mode:
        return False
    # Already finalized — nothing frozen to rescue.
    tail_type = state.events[-1].type
    if tail_type in ("final_text", "error"):
        return False
    if state.last_event_ts <= 0:
        return False
    when = now if now is not None else time.time()
    threshold = (
        STALL_FINALIZE_TOOL_USE_SECONDS
        if tail_type == "tool_use"
        else STALL_FINALIZE_AFTER_SECONDS
    )
    if (when - state.last_event_ts) < threshold:
        return False
    logger.warning(
        "stall_finalize user=%d sess=%s wid=%s idle=%.0fs tail=%s threshold=%.0fs",
        user_id,
        sess.id,
        sess.window_id,
        when - state.last_event_ts,
        tail_type,
        threshold,
        extra={
            "event": "stall_finalize",
            "user_id": user_id,
            "session_id": sess.id,
            "window_id": sess.window_id,
            "idle_seconds": round(when - state.last_event_ts),
            "tail_type": tail_type,
            "threshold_seconds": round(threshold),
        },
    )
    await finalize_task(bot, user_id, sess, STALL_NOTE)
    # Arm the false-positive recovery: if a real assistant turn arrives
    # after this, the next ``update_session_card`` / ``finalize_task``
    # spawns a fresh card below the stalled stub instead of silently
    # editing it. ``finalize_task`` already ran and re-fetched ``state``,
    # so re-read from ``_cards`` to set the flag on the same instance.
    post_state = _cards.get((user_id, sess.id))
    if post_state is not None:
        post_state.stall_finalized = True
    return True


def is_active_for_user(user_id: int, sess: Session) -> bool:
    active = session_manager.get_active_session(user_id)
    return active is not None and active.id == sess.id


async def repost_card(bot: Bot, user_id: int, sess: Session) -> None:
    """Send a fresh live-card below the user's latest message, and drop
    the previous one if it exists.

    Called from text_handler on every user-msg dispatch (the legacy
    ``card_position`` setting was retired; always-repost is now the
    single canonical behaviour). The user always sees a bot-side card
    immediately below the message they just typed instead of having
    to wait for claude's first event — which may come seconds later
    when the model spends a while in thinking before any tool call.

    No-op only when the card is paused (Menu / sub-screen open). In all
    other cases — including the post-finalize_task state where the
    previous live card was already pinned + reset — we seed a fresh
    card so it lands below the user's typed line. When claude's first
    event arrives it will edit *this* card (state.msg_id is now set)
    instead of spawning a separate one above the user msg.
    """
    state = _cards.get((user_id, sess.id))
    if state is not None and state.in_menu_view:
        return
    state = get_card_state(user_id, sess)
    # Seed history from JSONL on first call after a bot restart so the
    # reposted card lands with full context, not an empty body.
    await _ensure_seeded(user_id, sess, state)
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
        state.pending_edit = None

    # Lock the msg_id mutation + spawn so a parallel
    # ``update_session_card`` (for a claude event arriving mid-typing)
    # can't see the brief ``msg_id is None`` window and spawn its own
    # card too — Task #50.
    async with _card_lock(user_id, sess.id):
        old_msg_id = state.msg_id
        state.msg_id = None  # force _send_card to create a fresh message

        text = _render_card(sess, state, user_id=user_id)
        await _send_card(bot, user_id, sess, state, text=text)
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
        # A freshly (re)posted card is brand new — reset the freshness
        # clock so the first arriving claude event can't misjudge it as
        # stale and spawn a SECOND card ~1-2s later (the delete+resend
        # flicker). ``repost_card`` previously updated last_rendered /
        # last_edit_ts but left ``last_event_ts`` pinned to the previous
        # turn; on a card idle >= STALE_CARD_SECONDS that tripped
        # ``_is_stale`` on the very next event. A repost is itself user
        # activity, so "now" is the correct freshness stamp.
        state.last_event_ts = time.time()
        new_msg_id = state.msg_id
    logger.info(
        "repost_card user=%s sess=%s old_msg=%s new_msg=%s events=%d",
        user_id,
        sess.id,
        old_msg_id,
        new_msg_id,
        len(state.events),
    )
    if old_msg_id and new_msg_id and new_msg_id != old_msg_id:
        try:
            await bot.delete_message(chat_id=user_id, message_id=old_msg_id)
            logger.info(
                "repost_card deleted_old user=%s sess=%s msg=%s",
                user_id,
                sess.id,
                old_msg_id,
            )
        except Exception as e:
            logger.warning(
                "repost_card delete_old_failed user=%s sess=%s msg=%s err=%s",
                user_id,
                sess.id,
                old_msg_id,
                e,
            )


async def refresh_panel(bot: Bot, user_id: int) -> None:
    """Re-render the active session's live card so the bg-status panel
    (and active quota glyph) reflects the latest bg_status state.

    No-op when:
      - the user has no active session
      - the active session has no live card yet
      - the card is paused (menu/sub-screen open)
      - a deferred edit is already queued — that edit will pick up the
        latest panel state on its own when it fires
    """
    active = session_manager.get_active_session(user_id)
    if active is None:
        return
    state = _cards.get((user_id, active.id))
    if state is None or state.msg_id is None or state.in_menu_view:
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        return
    text = _render_card(active, state, user_id=user_id)
    if text == state.last_rendered:
        return
    if await _edit_card(bot, user_id, state, text=text):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()


# ─── Tool-timer tick ──────────────────────────────────────────────────

# How often to re-render the active card to advance the ⏳ M:SS counter
# on the latest in-flight tool/thinking entry. Matches the
# session_monitor poll cadence (2 s) so the card feels as responsive as
# Telegram's own "typing…" indicator — per-user feedback on pivot #39.
# Inline-screenshot cards are additionally throttled by
# ``_PHOTO_EDIT_MIN_INTERVAL`` (2.5 s) so editMessageMedia bursts stay
# within Telegram's limits.
CARD_TIMER_TICK_SECONDS = 2.0


async def card_timer_loop(bot: Bot) -> None:
    """Background task that ticks the elapsed timer on the latest
    in-flight tool/thinking entry of each user's active card.

    Skips:
      - cards with no msg_id
      - paused cards (in_menu_view)
      - users whose pagination puts them on a non-latest page (timer
        only ticks on the page where the in-flight event lives, i.e.
        the latest page)
      - cards with a pending deferred edit (the deferred edit picks up
        the updated timer when it fires)
    """
    logger.info("card_timer_loop started tick=%.1fs", CARD_TIMER_TICK_SECONDS)
    while True:
        try:
            await asyncio.sleep(CARD_TIMER_TICK_SECONDS)
            for (uid, sid), state in list(_cards.items()):
                try:
                    if state.msg_id is None or state.in_menu_view:
                        continue
                    sess = session_manager.get_session(sid)
                    if sess is None:
                        continue
                    # Only the user's currently-active session ticks.
                    active = session_manager.get_active_session(uid)
                    if active is None or active.id != sid:
                        continue
                    pages = paginate_events_for_card(state, uid)
                    idx = _resolved_page_idx(state, len(pages))
                    # Timer renders only on the latest page.
                    if idx != len(pages) - 1:
                        continue
                    if _latest_inflight_idx(pages[idx]) is None:
                        continue
                    # Skip when an edit is already queued — it'll pick
                    # up the fresh timer value when it fires.
                    if state.pending_edit is not None and not state.pending_edit.done():
                        continue
                    text = _render_card(sess, state, user_id=uid)
                    if text == state.last_rendered:
                        continue
                    if await _edit_card(bot, uid, state, text=text):
                        state.last_rendered = text
                        state.last_edit_ts = time.monotonic()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("card_timer tick failed for sess=%s: %s", sid, e)
        except asyncio.CancelledError:
            logger.info("card_timer_loop cancelled")
            break
        except Exception as e:
            logger.warning("card_timer_loop error: %s", e)
