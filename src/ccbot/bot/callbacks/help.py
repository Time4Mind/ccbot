"""Help-screen callbacks: CB_HLP_HOME (top-level) + CB_HLP_SEC (section).

Lives in its own module so the dispatcher in ``callbacks/__init__.py``
can route by prefix without a fat conditional. The actual rendering
(``render_help``) lives in ``bot/commands/info.py`` next to
``help_command`` so both the /help slash entry and the inline taps
share one source of truth.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...handlers.callback_data import CB_HLP_HOME, CB_HLP_SEC
from ...handlers.message_sender import safe_edit
from ..commands.info import HELP_SECTIONS, render_help

logger = logging.getLogger(__name__)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    del context  # callback uses no per-user state
    data = query.data or ""

    if data == CB_HLP_HOME:
        text, keyboard = render_help(user.id, "home")
        try:
            await safe_edit(query, text, reply_markup=keyboard)
        except Exception as e:
            logger.debug("help home edit failed: %s", e)
        await query.answer()
        return True

    if data.startswith(CB_HLP_SEC):
        section = data[len(CB_HLP_SEC) :]
        if section not in HELP_SECTIONS:
            await query.answer("Unknown section")
            return True
        text, keyboard = render_help(user.id, section)
        try:
            await safe_edit(query, text, reply_markup=keyboard)
        except Exception as e:
            logger.debug("help section edit failed: %s", e)
        await query.answer()
        return True

    return False
