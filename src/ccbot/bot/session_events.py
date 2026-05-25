"""Outbound routing — dispatch claude → TG events to live session cards.

A single ``handle_new_message`` is registered with ``SessionMonitor``;
each emitted ``NewMessage`` resolves the owning Session, updates that
session's live card, and on terminal-text turns calls
``finalize_task`` for the completion summary.

Also handles:
  - empty-content filter (Claude sometimes emits placeholder text/thinking
    chunks after /model or /clear; they'd ghost-edit the card without this).
  - ``INTERACTIVE_TOOL_NAMES`` short-circuit — those tools render their own
    prompt UI in TG instead of going through the live card.
  - G6 quota crossings — separate push, not card-merged.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Bot

from ..config import config
from ..handlers import bg_status
from ..handlers.interactive_ui import (
    INTERACTIVE_TOOL_NAMES,
    clear_interactive_msg,
    get_interactive_msg_id,
)
from ..handlers.notifications import (
    finalize_task,
    is_active_for_user,
    refresh_panel,
    update_session_card,
)
from ..handlers.typing import fire_typing
from ..session import session_manager
from ..session_monitor import NewMessage
from ..terminal_parser import extract_interactive_content
from ..tmux_manager import tmux_manager
from ..usage import context_pct_for_session

logger = logging.getLogger(__name__)


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Route one assistant turn (or streaming chunk) into the right live card."""
    logger.info(
        "claude_message",
        extra={
            "event": "claude_message",
            "session_id": msg.session_id,
            "status": "complete" if msg.is_complete else "streaming",
            "role": msg.role,
            "content_type": msg.content_type,
            "text_len": len(msg.text),
        },
    )

    targets = session_manager.all_user_sessions_with_claude_id(msg.session_id)
    if not targets:
        # Try to bind via the session_map (claude_session_id -> window_id) when a
        # Session exists for the matching window without a claude_session_id yet.
        await session_manager.load_session_map()
        targets = session_manager.all_user_sessions_with_claude_id(msg.session_id)
    if not targets:
        logger.info("No session record for claude session %s", msg.session_id)
        return

    # Drop empty assistant placeholder turns (Claude sometimes emits them
    # right after /model or /clear) — they'd ghost-edit the card.
    if (
        msg.role == "assistant"
        and msg.content_type in ("text", "thinking")
        and not (msg.text or "").strip()
    ):
        logger.debug(
            "Dropping empty assistant %s for session=%s",
            msg.content_type,
            msg.session_id,
        )
        return

    for user_id, sess in targets:
        wid = sess.window_id
        if not wid:
            continue
        session_manager.touch_session(sess.id)
        is_active = is_active_for_user(user_id, sess)

        # Drive Telegram's "typing…" indicator from real claude activity.
        # Telegram refreshes the indicator every ~5s, so as long as the
        # active session keeps emitting events, the user sees "typing"
        # in the chat header; the indicator naturally fades within ~5s
        # once events stop. Bg sessions skip — they don't surface in
        # the chat header.
        if is_active:
            await fire_typing(
                bot,
                user_id,
                "session_events",
                session_id=sess.id,
                content_type=msg.content_type,
            )

        if not config.show_tool_calls and msg.content_type in (
            "tool_use",
            "tool_result",
        ):
            continue

        # Tools that render their own UI go through the interactive-UI surface.
        if msg.tool_name in INTERACTIVE_TOOL_NAMES and msg.content_type == "tool_use":
            if not is_active:
                # Background session: never show the UI in chat — instead
                # snapshot the prompt and flip the bg-status badge to ❓.
                # The switcher-tap handler renders the snapshot when the
                # user opens the session.
                await asyncio.sleep(0.3)
                w = await tmux_manager.find_window_by_id(wid)
                ui_tuple: tuple[str, str] | None = None
                if w:
                    pane_text = await tmux_manager.capture_pane(w.window_id)
                    if pane_text:
                        ui = extract_interactive_content(pane_text)
                        if ui is not None:
                            ui_tuple = (ui.content, ui.name)
                if bg_status.update_status(
                    user_id, sess.id, "needs_action", interactive_ui=ui_tuple
                ):
                    await refresh_panel(bot, user_id)
                    # Bg push notification (Task #42): only on TRANSITION
                    # into needs_action (update_status returned True). The
                    # user-setting toggles this — default on.
                    if session_manager.get_user_settings(user_id).get(
                        "bg_notify_needs_action", True
                    ):
                        from ..handlers.notifications import push_event

                        try:
                            await push_event(
                                bot, user_id, sess, text="needs your attention"
                            )
                        except Exception as e:
                            logger.debug("bg needs_action push failed: %s", e)
                continue

            # Active session needs_action: card msg edits to kb-mode
            # view (no separate push). Task #41 / Pivot kb-mode v2.
            from ..handlers.notifications import enter_kb_mode

            await asyncio.sleep(0.3)
            w = await tmux_manager.find_window_by_id(wid)
            if w:
                pane_text = await tmux_manager.capture_pane(w.window_id)
                if pane_text:
                    ui = extract_interactive_content(pane_text)
                    if ui is not None:
                        await enter_kb_mode(bot, user_id, sess, ui.content, ui.name)
                        claude_sess = await session_manager.resolve_session_for_window(
                            wid
                        )
                        if claude_sess and claude_sess.file_path:
                            try:
                                file_size = Path(claude_sess.file_path).stat().st_size
                                session_manager.update_user_window_offset(
                                    user_id, wid, file_size
                                )
                            except OSError:
                                pass
                        continue
            # Pane parse failed — fall through to regular card update.

        # Any non-interactive event invalidates a previously-shown interactive UI.
        if get_interactive_msg_id(user_id, wid):
            await clear_interactive_msg(user_id, bot, wid)
        # Same for the kb-mode card view — claude has moved past the
        # prompt, so flip the card back to its regular layout.
        from ..handlers.notifications import exit_kb_mode, has_pending_kb

        has_prompt, in_kb = has_pending_kb(user_id, sess.id)
        if has_prompt or in_kb:
            await exit_kb_mode(bot, user_id, sess, clear_pending=True)

        if msg.is_complete:
            # Real end-of-turn assistant text → "task complete".  Mid-stream
            # text blocks (stop_reason=tool_use) are intermediate narration —
            # those belong on the live card, not the completion summary.
            is_terminal_text = (
                msg.role == "assistant"
                and msg.content_type == "text"
                and msg.stop_reason in ("end_turn", "stop_sequence", "max_tokens")
            )
            # The live-card calls below silently buffer (in_menu_view=True
            # is already set on bg sessions by the carrier-transfer path)
            # so a switch-back later renders the full tool history. The
            # bg-status badge below is the chat-visible signal while bg.
            if is_terminal_text:
                await finalize_task(bot, user_id, sess, msg.text or "")
            else:
                await update_session_card(bot, user_id, sess, msg)

            if not is_active:
                new_status: bg_status.Status = (
                    "finished" if is_terminal_text else "working"
                )
                if bg_status.update_status(user_id, sess.id, new_status):
                    await refresh_panel(bot, user_id)
                    # Bg push (Task #42): only on TRANSITION into finished.
                    # ``update_status`` returns True only on actual change
                    # — natural dedup, won't spam if same state re-affirms.
                    if new_status == "finished" and session_manager.get_user_settings(
                        user_id
                    ).get("bg_notify_finished", True):
                        from ..handlers.notifications import push_event

                        try:
                            await push_event(bot, user_id, sess, text="task complete")
                        except Exception as e:
                            logger.debug("bg finished push failed: %s", e)

            claude_sess = await session_manager.resolve_session_for_window(wid)
            if claude_sess and claude_sess.file_path:
                try:
                    file_size = Path(claude_sess.file_path).stat().st_size
                    session_manager.update_user_window_offset(user_id, wid, file_size)
                except OSError:
                    pass

            # Context-pct refresh from JSONL on every end-of-turn
            # assistant text. The /context-command-based poller was
            # disabled because it pollutes the session's JSONL (modal
            # output gets written back as a fake user turn). JSONL math
            # is non-invasive — see ``usage.context_pct_for_session``.
            if msg.role == "assistant" and msg.content_type == "text":
                try:
                    pct = await context_pct_for_session(sess)
                except Exception as e:
                    logger.debug("context pct fetch failed: %s", e)
                    pct = None
                if pct is not None:
                    from ..handlers.bg_status import set_context_pct
                    from ..handlers.notifications import set_card_context_pct

                    set_card_context_pct(user_id, sess.id, pct)
                    set_context_pct(user_id, sess.id, pct)
                    await refresh_panel(bot, user_id)
        else:
            # Streaming chunk — best-effort card update.
            await update_session_card(bot, user_id, sess, msg)
            if not is_active:
                if bg_status.update_status(user_id, sess.id, "working"):
                    await refresh_panel(bot, user_id)
