"""Telegram update hook for the adaptive live-card activity clock."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from ..user_activity import record
from ._common import is_user_allowed


async def record_user_message_activity(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Record every allowed user message without consuming the update."""
    user = update.effective_user
    if user is not None and is_user_allowed(user.id):
        record(user.id)
