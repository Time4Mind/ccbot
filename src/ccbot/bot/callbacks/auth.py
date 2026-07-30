"""CB_AUTH_* — buttons of the Claude re-login flow.

🔐 Login starts the OAuth exchange (same code path as ``/login``); Cancel drops
a pending flow so its child process doesn't sit around holding a pty.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...claude_auth import drop_flow
from ...codex_auth import cancel_flow
from ...handlers.callback_data import CB_AUTH_CANCEL, CB_AUTH_LOGIN
from ...handlers.message_sender import safe_send
from ...i18n import t
from ..commands.auth import start_login

logger = logging.getLogger(__name__)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    """Claim CB_AUTH_* callbacks. Returns True when handled."""
    data = query.data or ""

    if data == CB_AUTH_LOGIN:
        await query.answer()
        await start_login(context.bot, user.id)
        return True

    if data == CB_AUTH_CANCEL:
        await query.answer()
        drop_flow(user.id)
        await cancel_flow(user.id)
        await safe_send(context.bot, user.id, t(user.id, "auth.login.cancelled"))
        return True

    return False
