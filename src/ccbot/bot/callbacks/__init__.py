"""Inline-keyboard callback dispatcher.

``callback_handler`` reads ``query.data`` and tries each per-prefix
sub-module in turn — the first to claim the data wins. Adding a new
callback prefix means dropping a new function in the right sub-module
and including it in this dispatcher's chain.
"""

from __future__ import annotations

import logging

from telegram import Bot, Update
from telegram.ext import ContextTypes

from .._common import is_user_allowed
from ...handlers.notifications import get_card_state, refresh_panel
from ...session import session_manager
from ...user_activity import record as record_user_activity
from . import (
    archive,
    auth as auth_callbacks,
    confirm,
    dir_browser,
    file_buttons,
    footer,
    help as help_callbacks,
    history_pagination,
    interactive_ui,
    more_menu,
    nodes,
    settings as settings_callbacks,
    switcher,
    transfer,
    window_picker,
)

logger = logging.getLogger(__name__)


def _acknowledge_completion_marker(
    user_id: int, query: object
) -> tuple[str, int] | None:
    """Acknowledge a completion marker only from its own live carrier."""
    message = getattr(query, "message", None)
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return None
    active = session_manager.get_active_session(user_id)
    if active is None:
        return None
    state = get_card_state(user_id, active)
    if state.msg_id != message_id or not state.completion_marker_pending:
        return None
    state.completion_marker_pending = False
    return active.id, message_id


async def _repaint_acknowledged_marker(
    bot: Bot, user_id: int, acknowledged: tuple[str, int] | None
) -> None:
    """Remove the marker visually if the tap left its live card in place."""
    if acknowledged is None:
        return
    session_id, message_id = acknowledged
    active = session_manager.get_active_session(user_id)
    if active is None or active.id != session_id:
        return
    state = get_card_state(user_id, active)
    if state.msg_id != message_id or state.in_menu_view:
        return
    await refresh_panel(bot, user_id, immediate=True)


# Order matters only for prefix overlap; in practice the prefixes are disjoint.
_HANDLERS = (
    history_pagination.handle,
    auth_callbacks.handle,
    dir_browser.handle,
    file_buttons.handle,
    window_picker.handle,
    switcher.handle,
    nodes.handle,
    transfer.handle,
    archive.handle,
    footer.handle,
    more_menu.handle,
    settings_callbacks.handle,
    confirm.handle,
    interactive_ui.handle,
    help_callbacks.handle,
)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level dispatcher invoked by ``CallbackQueryHandler``."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        # Silently dismiss the spinner — no "Not authorized" toast. The
        # allowlist is private; unauthorized users should see the bot as
        # inert (a callback that just goes nowhere) instead of getting
        # a "you found the bot, just not the user" signal.
        try:
            await query.answer()
        except Exception:
            pass
        return

    # Every allowed button tap is an explicit user action, including noop
    # buttons that only dismiss Telegram's spinner.
    record_user_activity(user.id)
    acknowledged = _acknowledge_completion_marker(user.id, query)

    if query.data == "noop":
        await query.answer()
        await _repaint_acknowledged_marker(context.bot, user.id, acknowledged)
        return

    for h in _HANDLERS:
        if await h(query, context, user):
            await _repaint_acknowledged_marker(context.bot, user.id, acknowledged)
            return

    logger.debug("unhandled callback data: %s", query.data)
    await query.answer()
    await _repaint_acknowledged_marker(context.bot, user.id, acknowledged)
