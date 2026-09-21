"""Send a file selected through an inline Rich Markdown button."""

from __future__ import annotations

import logging
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...file_actions import (
    FILE_CALLBACK_PREFIX,
    resolve_file_reference,
)
from ...file_delivery import SubmitResult, file_delivery_manager

logger = logging.getLogger(__name__)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""
    if not data.startswith(FILE_CALLBACK_PREFIX):
        return False

    reference = resolve_file_reference(data[len(FILE_CALLBACK_PREFIX) :])
    if reference is None:
        await query.answer("Файл больше недоступен", show_alert=True)
        return True

    try:
        await query.answer()
        result = file_delivery_manager.submit(
            bot=context.bot,
            user_id=user.id,
            delivery_key=data[len(FILE_CALLBACK_PREFIX) :],
            reference=reference,
        )
        if result in (SubmitResult.USER_BUSY, SubmitResult.STOPPED):
            logger.info(
                "file delivery not scheduled user=%s reason=%s", user.id, result
            )
            if result is SubmitResult.USER_BUSY:
                file_delivery_manager.notify_user(
                    bot=context.bot,
                    user_id=user.id,
                    text="Другой файл уже отправляется. Дождитесь его завершения.",
                )
    except Exception as exc:
        logger.warning(
            "file button scheduling failed user=%s error_type=%s",
            user.id,
            type(exc).__name__,
        )
    return True
