"""Send a file selected through an inline Rich Markdown button."""

from __future__ import annotations

import logging
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...file_actions import FILE_CALLBACK_PREFIX, resolve_file_button

logger = logging.getLogger(__name__)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""
    if not data.startswith(FILE_CALLBACK_PREFIX):
        return False

    path = resolve_file_button(data[len(FILE_CALLBACK_PREFIX) :])
    if path is None:
        await query.answer("Файл больше недоступен", show_alert=True)
        return True

    await query.answer()
    try:
        with path.open("rb") as source:
            await context.bot.send_document(
                chat_id=user.id,
                document=source,
                filename=path.name,
                disable_notification=True,
            )
    except Exception as exc:
        logger.warning("file button send failed path=%s: %s", path, exc)
        await context.bot.send_message(
            chat_id=user.id,
            text="Не удалось отправить файл.",
            disable_notification=True,
        )
    return True
