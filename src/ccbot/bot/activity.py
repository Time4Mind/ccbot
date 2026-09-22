"""Telegram update hook for the adaptive live-card activity clock."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from ..handlers.notifications import get_card_state, refresh_panel
from ..session import session_manager
from ..user_activity import record
from ._common import is_user_allowed


async def record_user_message_activity(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Record every allowed user message without consuming the update."""
    user = update.effective_user
    if user is not None and is_user_allowed(user.id):
        record(user.id)
        active = session_manager.get_active_session(user.id)
        if active is not None:
            # The header acknowledgement is deliberately independent from the
            # switcher button's two-presentation unread lifecycle. Any user
            # message acknowledges what is already visible on the active card.
            state = get_card_state(user.id, active)
            if state.completion_marker_pending:
                state.completion_marker_pending = False
                # Keep user-message routing off Telegram edit latency. PTB
                # owns the repaint task and reports failures through its
                # regular error path; menu transitions can safely make this
                # refresh a no-op by pausing the card first.
                context.application.create_task(
                    refresh_panel(context.bot, user.id, immediate=True),
                    update=update,
                )
