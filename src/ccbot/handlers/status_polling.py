"""Terminal status line polling for active and idle sessions.

Provides background polling of terminal status lines for all the bot's
single user's live sessions:
  - Detects Claude Code status (working, waiting, etc.) and drives the
    Telegram ``typing…`` indicator
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Reaps tmux windows that vanished externally — marks Session as `lost`
    and cleans up in-memory state

Key components:
  - STATUS_POLL_INTERVAL: polling frequency (1 s)
  - status_poll_loop: background polling task
  - update_status_message: poll a single window for UI + typing signal
"""

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

from telegram import Bot
from telegram.constants import ChatAction

from ..config import config
from ..session import session_manager

if TYPE_CHECKING:
    from ..session_models import Session
from ..terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
    parse_status_line,
)
from ..tmux_manager import tmux_manager
from . import bg_status
from .archive import idle_archive_sweep, purge_sweep
from .cleanup import clear_session_state
from .inbox import inbox_sweep
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .notifications import (
    is_card_busy,
    is_card_finalized,
    is_card_in_menu_view,
    maybe_finalize_stalled,
    refresh_panel,
)

# Match option lines like "  1. Yes" / " ❯ 2. Yes, and don't ask again".
_OPTION_LINE_RE = re.compile(r"^[\s❯>]*?(\d+)\.\s+(.+?)\s*$")


def _parse_first_yes_option(pane_text: str) -> str | None:
    """First option line whose label starts with "Yes". Returns its number."""
    for raw in pane_text.splitlines():
        m = _OPTION_LINE_RE.match(raw)
        if not m:
            continue
        if m.group(2).lower().startswith("yes"):
            return m.group(1)
    return None


async def _maybe_auto_approve(user_id: int, window_id: str, pane_text: str) -> bool:
    """Auto-Yes on the in-pane Yes/No prompt when the user opted in.

    Returns True iff a key was sent — caller should then skip surfacing the
    UI to TG.
    """
    mode = session_manager.get_user_settings(user_id).get("auto_approve", "off")
    if mode != "on":
        return False
    digit = _parse_first_yes_option(pane_text)
    if digit is None:
        return False
    # Number-key shortcut: typing the digit picks the option, Enter submits.
    try:
        await tmux_manager.send_keys(window_id, digit, enter=True)
    except Exception as e:
        logger.debug("auto_approve send_keys failed: %s", e)
        return False
    logger.info(
        "Auto-approved interactive prompt (opt=%s) for user=%d window=%s",
        digit,
        user_id,
        window_id,
    )
    return True


logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds — fast feedback; rate limiting is at send layer.

# Per-session cache of the last status-line seen by ``parse_status_line``,
# used to tell "active spinner that just changed text" from "frozen
# scrollback line that hasn't been pushed off yet". A genuinely-busy
# claude updates the elapsed time roughly every second
# (``Working… (17s)`` → ``(18s)`` → …). When the same line repeats
# across polls for more than ``_PANE_STATUS_STALE_AFTER`` seconds we
# treat it as stale and stop firing TYPING off of it.
_pane_status_cache: dict[tuple[int, str], tuple[str, float]] = {}
_PANE_STATUS_STALE_AFTER = 3.0


def _pane_status_is_changing(user_id: int, key_suffix: str, status_line: str) -> bool:
    """True iff the status line we just parsed differs from the last
    one OR was first seen within the staleness window. Side-effect:
    updates the cache when the line changes."""
    key = (user_id, key_suffix)
    prev_line, prev_ts = _pane_status_cache.get(key, ("", 0.0))
    now = time.time()
    if status_line != prev_line:
        _pane_status_cache[key] = (status_line, now)
        return True
    return (now - prev_ts) < _PANE_STATUS_STALE_AFTER


# Idle-archive sweep cadence (seconds).
ARCHIVE_SWEEP_INTERVAL = 60.0
# Archive-purge sweep cadence (seconds).
PURGE_SWEEP_INTERVAL = 3600.0


async def _resolve_existing_interactive(
    bot: Bot,
    user_id: int,
    window_id: str,
    pane_text: str,
    interactive_window: str | None,
) -> bool | None:
    """Resolve an interactive UI already claimed for THIS window.

    Returns ``None`` when the caller should stop (UI still showing — skip
    this poll). Otherwise returns the ``should_check_new_ui`` flag the
    caller should carry forward: ``True`` if no prior interactive mode was
    active for this window, ``False`` if it was just cleared.
    """
    if interactive_window != window_id:
        return True
    # User is in interactive mode for THIS window
    if is_interactive_ui(pane_text):
        # Interactive UI still showing — skip status update (user is interacting)
        return None
    # Interactive UI gone — clear interactive mode, fall through.
    await clear_interactive_msg(user_id, bot, window_id)
    return False


async def _surface_new_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    pane_text: str,
    sess: "Session | None",
    is_bg_session: bool,
    interactive_window: str | None,
) -> bool:
    """Classify and surface a fresh interactive UI found by polling.

    Handles auto-approve, the bg-session stash, the busy-elsewhere skip,
    and the active-session kb-mode / floating-msg routes. Returns ``True``
    iff the prompt was handled — the caller should then ``return``.

    ``is_interactive_ui(pane_text)`` is assumed True by the caller.
    """
    # User-configurable auto-approve takes precedence — bypass both
    # the TG surface AND the bg-status badge when the user has
    # explicitly opted in to "Yes on everything". This must run
    # BEFORE the bg-session branch — otherwise a background prompt
    # would sit pending forever with ❓ instead of getting the
    # auto-Yes the user asked for. After approval the next poll
    # sees no UI and clears the badge naturally.
    if await _maybe_auto_approve(user_id, window_id, pane_text):
        return True

    if is_bg_session and sess is not None:
        # Background session: prompt didn't qualify for auto-approve
        # (e.g. no "Yes" option, or feature off). Never surface in
        # chat — stash the snapshot in bg_status and flip ❓ on the
        # panel. The switcher-tap handler renders it when the user
        # looks at the session.
        content_obj = extract_interactive_content(pane_text)
        ui_tuple = (
            (content_obj.content, content_obj.name) if content_obj is not None else None
        )
        if bg_status.update_status(
            user_id, sess.id, "needs_action", interactive_ui=ui_tuple
        ):
            await refresh_panel(bot, user_id)
        return True
    if interactive_window is not None:
        # User is already engaged with another window's UI; don't
        # double-surface. The prompt stays pending in the pane and
        # will get picked up on the next poll cycle after the user
        # clears the current one.
        logger.debug(
            "Interactive UI on window=%s, but user busy on window=%s — "
            "skipping TG surface for now",
            window_id,
            interactive_window,
        )
        return True
    logger.debug(
        "Interactive UI detected in polling (user=%d, window=%s)",
        user_id,
        window_id,
    )
    # Active session: route via kb-mode on the live card. Falls
    # back to the floating-msg path only when there's no Session
    # record (orphan window — no card to host kb-mode).
    if sess is not None and not is_bg_session:
        from .notifications import enter_kb_mode

        content_obj = extract_interactive_content(pane_text)
        if content_obj is not None:
            await enter_kb_mode(
                bot, user_id, sess, content_obj.content, content_obj.name
            )
            return True
    await handle_interactive_ui(bot, user_id, window_id)
    return True


async def _reconcile_no_ui_state(
    bot: Bot,
    user_id: int,
    pane_text: str,
    sess: "Session | None",
    is_bg_session: bool,
) -> None:
    """Reconcile card / bg-status when no interactive UI is on the pane.

    Clears a stale bg-session ❓ stash and flips the active card out of a
    dismissed slash-command kb-mode picker.
    """
    # No interactive UI on this pane right now. If we previously stashed
    # one for a bg session (e.g. claude dismissed the prompt without our
    # input), clear it so the ❓ badge doesn't lie.
    if is_bg_session and sess is not None and not is_interactive_ui(pane_text):
        if bg_status.clear_pending_ui(user_id, sess.id):
            await refresh_panel(bot, user_id)

    # Active session was in kb-mode for a slash-command picker that
    # has just dismissed (no JSONL event fires for /model | /effort |
    # etc., so this is the only place the card-mode flip can happen).
    if sess is not None and not is_bg_session:
        from .notifications import exit_kb_mode, has_pending_kb

        has_prompt, in_kb = has_pending_kb(user_id, sess.id)
        if has_prompt or in_kb:
            await exit_kb_mode(bot, user_id, sess, clear_pending=True)


async def _drive_typing_indicator(
    bot: Bot,
    user_id: int,
    window_id: str,
    pane_text: str,
    sess: "Session | None",
    is_bg_session: bool,
) -> None:
    """Compute busy signals, run the stalled-session rescue, and fire
    the Telegram ``typing…`` chat-action for the active session."""
    # Telegram chat-action "typing…" — fired every poll cycle for the
    # active session while it's busy. Two signals combine:
    #
    #   * ``is_card_busy``: a recent claude event arrived (within
    #     ``2 × CARD_EDIT_LAG``) AND the tail event isn't a terminal
    #     one. Bridges intra-turn gaps and prevents post-completion
    #     stickiness.
    #   * pane spinner (``parse_status_line``): claude TUI is showing
    #     ``⠋ Working…``. Picks up the long-thinking case where no
    #     JSONL events arrive for 20+ s — without this the indicator
    #     would go dark even though claude is genuinely working.
    #
    # Both gated on ``not is_bg_session`` (bg sessions don't surface
    # the chat-header typing badge) and not in_menu_view (user is
    # browsing menu screens; typing there is noise).
    status_line = parse_status_line(pane_text) or ""
    pane_busy = bool(status_line) and _pane_status_is_changing(
        user_id, sess.id if sess else window_id, status_line
    )
    in_menu = sess is not None and is_card_in_menu_view(user_id, sess.id)
    card_busy = sess is not None and is_card_busy(user_id, sess.id)
    # When the card is finalized (last event = ``final_text`` /
    # ``error``), pane_busy is a lie — the spinner line is just
    # scrollback that hasn't scrolled off yet (observed: ``Sautéed
    # for 11m 16s · 1 shell still running`` sticks around after the
    # turn ends because a background shell is still attached). Trust
    # the JSONL signal in that case.
    if pane_busy and sess is not None and is_card_finalized(user_id, sess.id):
        pane_busy = False

    # Stalled-session rescue (bug A4). For the ACTIVE session only: if the
    # card has a non-terminal tail event but the pane spinner is idle and
    # no new event has arrived for STALL_FINALIZE_AFTER_SECONDS, the
    # upstream claude process likely stalled/exited (it may still write
    # ``last-prompt`` / ``ai-title`` metadata, which transcript_parser
    # filters out, so the monitor produces no card update). Finalise the
    # frozen card with a clear note instead of leaving it stuck forever.
    # Excluded: a still-changing spinner, a waiting interactive UI / kb
    # prompt, and menu navigation — all valid "idle" states, not stalls.
    if sess is not None and not is_bg_session:
        from .notifications import has_pending_kb

        interactive_waiting = (
            is_interactive_ui(pane_text)
            or get_interactive_window(user_id) == window_id
            or has_pending_kb(user_id, sess.id)[0]
        )
        await maybe_finalize_stalled(
            bot,
            user_id,
            sess,
            pane_busy=pane_busy,
            interactive_waiting=interactive_waiting,
            in_menu=in_menu,
        )

    if not is_bg_session and not in_menu and (card_busy or pane_busy):
        try:
            await bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
            logger.info(
                "typing_fired source=status_polling user=%d sess=%s wid=%s "
                "card_busy=%s pane_busy=%s status=%r",
                user_id,
                sess.id if sess else "-",
                window_id,
                card_busy,
                pane_busy,
                status_line[:40] if status_line else "",
                extra={
                    "event": "typing_fired",
                    "source": "status_polling",
                    "user_id": user_id,
                    "session_id": sess.id if sess else None,
                    "window_id": window_id,
                    "card_busy": card_busy,
                    "pane_busy": pane_busy,
                    "status_line": status_line[:80] if status_line else "",
                },
            )
        except Exception as e:
            logger.debug("send_chat_action TYPING failed: %s", e)


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
) -> None:
    """Poll terminal: detect interactive UIs and drive the typing indicator.

    There is no separate "status message" anymore — the live session card
    carries its own header. The pane spinner ("…Esc to interrupt") is no
    longer shown in chat; it drives ``send_chat_action(TYPING)`` instead.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return

    interactive_window = get_interactive_window(user_id)
    should_check_new_ui = await _resolve_existing_interactive(
        bot, user_id, window_id, pane_text, interactive_window
    )
    if should_check_new_ui is None:
        return

    sess = session_manager.find_session_by_window(window_id)
    active = session_manager.get_active_session(user_id)
    # Treat orphan windows (no session record) as "active-like": there's
    # no bg-status row to flip, so falling through to the legacy handler
    # is the safer default. Only suppress when we know this is a
    # background-session window for a different active session.
    is_bg_session = sess is not None and active is not None and active.id != sess.id

    # Check for permission prompt (interactive UI not triggered via JSONL).
    #
    # ``should_check_new_ui`` is False only when an earlier branch above
    # confirmed the user is currently engaged with THIS window's UI —
    # in that case we already returned. From here on we're looking at a
    # fresh prompt that polling, not the JSONL path, surfaced.
    #
    # The auto-approve check used to be gated on ``interactive_window is
    # None`` — a stale entry from any prior tool surfacing (or worse, a
    # stuck one that the cleanup path missed) would block auto-approve
    # for EVERY other window. ``auto_approve=on`` is an explicit opt-in;
    # respect it on every window, regardless of which prompt may also
    # be (or have been) pending elsewhere. The TG-surface step
    # (``handle_interactive_ui``) is the only place where stacking two
    # interactive screens is genuinely confusing, so the
    # ``interactive_window is None`` guard stays — but only there.
    if should_check_new_ui and is_interactive_ui(pane_text):
        if await _surface_new_interactive_ui(
            bot, user_id, window_id, pane_text, sess, is_bg_session, interactive_window
        ):
            return

    await _reconcile_no_ui_state(bot, user_id, pane_text, sess, is_bg_session)

    await _drive_typing_indicator(
        bot, user_id, window_id, pane_text, sess, is_bg_session
    )


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for every live session."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_archive_sweep = 0.0
    last_purge_sweep = 0.0
    while True:
        try:
            now = time.monotonic()

            # Idle-TTL sweep — archive sessions whose last_event_at exceeded TTL.
            if now - last_archive_sweep >= ARCHIVE_SWEEP_INTERVAL:
                last_archive_sweep = now
                for user_id in sorted(config.allowed_users):
                    try:
                        await idle_archive_sweep(bot, user_id)
                    except Exception as e:
                        logger.debug("idle_archive_sweep error: %s", e)

            # Long-archive purge sweep.
            if now - last_purge_sweep >= PURGE_SWEEP_INTERVAL:
                last_purge_sweep = now
                try:
                    purge_sweep()
                except Exception as e:
                    logger.debug("purge_sweep error: %s", e)
                try:
                    inbox_sweep()
                except Exception as e:
                    logger.debug("inbox_sweep error: %s", e)

            # Iterate every (user, window) pair derived from active+idle sessions.
            pairs: list[tuple[int, str]] = []
            for user_id in sorted(config.allowed_users):
                for sess in session_manager.list_user_sessions(
                    user_id, states=("active", "idle")
                ):
                    if sess.window_id:
                        pairs.append((user_id, sess.window_id))

            for user_id, wid in pairs:
                try:
                    # Reap tmux windows that vanished externally.
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        sess = session_manager.find_session_by_window(wid)
                        if sess is not None:
                            session_manager.mark_session_lost(sess.id)
                        await clear_session_state(user_id, wid, bot)
                        logger.info(
                            "Reaped lost window: user=%d window_id=%s",
                            user_id,
                            wid,
                        )
                        continue

                    # Keep the history-pages cache populated so the
                    # live-card's pagination counter has a stable value
                    # to render across streaming events (avoid the
                    # "blinking" keyboard where the counter appears
                    # and disappears). Throttled to once per 3s per
                    # window so the parse cost is bounded.
                    from .history import kick_prewarm

                    kick_prewarm(wid)

                    await update_status_message(bot, user_id, wid)
                except Exception as e:
                    logger.debug(
                        "Status update error for user %d window %s: %s",
                        user_id,
                        wid,
                        e,
                    )
        except Exception as e:
            logger.error("Status poll loop error: %s", e)

        await asyncio.sleep(STATUS_POLL_INTERVAL)
