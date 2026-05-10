"""Inline-keyboard callback dispatcher.

``callback_handler`` reads ``query.data`` and tries each per-prefix
sub-module in turn — the first to claim the data wins. Adding a new
callback prefix means dropping a new function in the right sub-module
and including it in this dispatcher's chain.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from .._common import is_user_allowed
from . import (
    archive,
    confirm,
    dir_browser,
    footer,
    history_pagination,
    interactive_ui,
    more_menu,
    screenshot_keys,
    settings as settings_callbacks,
    switcher,
    window_picker,
)

logger = logging.getLogger(__name__)


# Order matters only for prefix overlap; in practice the prefixes are disjoint.
_HANDLERS = (
    history_pagination.handle,
    dir_browser.handle,
    window_picker.handle,
    switcher.handle,
    archive.handle,
    footer.handle,
    more_menu.handle,
    settings_callbacks.handle,
    confirm.handle,
    interactive_ui.handle,
    screenshot_keys.handle,
)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level dispatcher invoked by ``CallbackQueryHandler``."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await query.answer("Not authorized")
        return

    if query.data == "noop":
        await query.answer()
        return

    for h in _HANDLERS:
        if await h(query, context, user):
            return

    logger.debug("unhandled callback data: %s", query.data)
    await query.answer()
