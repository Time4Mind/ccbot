"""Shared live-card registries, locks, message ownership, and repost intent state."""

from __future__ import annotations

from __future__ import annotations

import asyncio
import logging

from telegram import Bot

from ..session import session_manager
from .card_model import (
    CardState,
)

logger = logging.getLogger(__name__)


__all__ = [
    "_cards",
    "_card_surface_tasks",
    "_card_locks",
    "_card_lock",
    "_carrier_edit_locks",
    "_carrier_edit_lock",
    "_user_send_locks",
    "_user_send_lock",
    "_strip_stale_switchers",
    "_MSG_REGISTRY_LIMIT",
    "_msg_to_session",
    "_register_msg",
    "lookup_session_for_message",
    "reset_card_msg_id_for_user",
    "_inline_screens_enabled",
    "_should_buffer",
    "_repost_intent",
    "begin_repost_intent",
    "end_repost_intent",
    "reset_card",
    "_legacy",
]

# Per-(user, session.id) card state.
_cards: dict[tuple[int, str], CardState] = {}

# Fire-and-forget receipt surfaces started by Telegram intake. Keeping them in
# one registry lets shutdown cancel cleanly instead of leaving Telegram edits
# alive after the application has started closing its HTTP client.
_card_surface_tasks: set[asyncio.Task[bool]] = set()

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


# Per-user barrier for Telegram edits of the shared live-card carrier.
#
# A switch reuses the same Telegram message for another session.  The old
# session may already have an editMessageText request in flight when the user
# taps the switcher; cancelling its deferred task is then too late.  Without a
# barrier that old request can finish after the target session is painted and
# overwrite the carrier with the previous session's text.
#
# Every ``_edit_card`` holds this lock for the whole Telegram request.  The
# switch hand-off acquires it before pausing the old owner and flipping the
# active-session pointer, so all older edits finish first and all newer old-
# session edits observe ``in_menu_view=True`` before they can reach Telegram.
_carrier_edit_locks: dict[int, asyncio.Lock] = {}


def _carrier_edit_lock(user_id: int) -> asyncio.Lock:
    """Get-or-create the cross-session carrier-edit lock for one user."""
    lock = _carrier_edit_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _carrier_edit_locks[user_id] = lock
    return lock


# Per-user spawn lock. ``_send_card`` holds it across the whole
# "send the message → strip every other card's keyboard → record the new
# switcher carrier" sequence, so two *different sessions* spawning cards
# concurrently (a voice repost racing a typed-message repost) can't
# interleave those steps and leave the per-user ``last_switcher_msg_id``
# pointing at the older message — the desync behind "I tap the switcher
# on the last message but the previous one gets edited".
#
# ``_card_lock`` alone doesn't cover this: it is keyed per (user,
# session), so two sessions never contend on it. Lock order is always
# session-lock → user-lock; no path acquires them the other way round.
_user_send_locks: dict[int, asyncio.Lock] = {}


def _user_send_lock(user_id: int) -> asyncio.Lock:
    """Get-or-create the cross-session spawn-serialization lock for a user."""
    lock = _user_send_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_send_locks[user_id] = lock
    return lock


async def _strip_stale_switchers(
    bot: Bot, user_id: int, keep_msg_id: int, keep_session_id: str | None
) -> None:
    """Leave exactly ONE message in the chat carrying a live footer /
    switcher keyboard: ``keep_msg_id``.

    Stripping only ``last_switcher_msg_id`` (the previous behaviour) is
    not enough — that pointer is a single per-user slot, while every
    session owns its own card message and ``_edit_card`` re-attaches a
    keyboard on every edit without moving the pointer. Two live cards
    could therefore end up tappable at once, and a switcher tap would
    repaint whichever message the tap came from rather than the newest.

    So: strip the pointer's message *and* every other known card message
    for this user. Cards in kb-mode are skipped — their keyboard is the
    AskUserQuestion / ExitPlanMode navigation grid the user still has to
    act on, not a stale switcher.
    """
    targets: list[int] = []
    prev = session_manager.get_last_switcher_msg(user_id)
    if prev and prev != keep_msg_id:
        targets.append(prev)
    for (uid, sid), st in _cards.items():
        if uid != user_id or sid == keep_session_id or st.in_kb_mode:
            continue
        if st.msg_id is not None and st.msg_id != keep_msg_id:
            if st.msg_id not in targets:
                targets.append(st.msg_id)
    for msg_id in targets:
        try:
            await bot.edit_message_reply_markup(
                chat_id=user_id, message_id=msg_id, reply_markup=None
            )
        except Exception:
            # Already stripped / deleted / not editable — nothing to do.
            pass


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

    Called when the user toggles ``card_inline_screenshots``. We orphan
    the old carrier so the next event starts a fresh card below the next
    user message with the requested media layout.
    """
    for (uid, _sid), state in _cards.items():
        if uid != user_id:
            continue
        state.msg_id = None
        state.is_rich_media_msg = False
        state.rich_media_file_id = ""
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


def _legacy(name: str):
    """Resolve a patch-sensitive dependency from the notifications facade."""
    import sys

    facade = sys.modules.get("ccbot.handlers.notifications")
    if facade is None or not hasattr(facade, name):
        raise RuntimeError(f"notifications facade is missing {name}")
    return getattr(facade, name)
