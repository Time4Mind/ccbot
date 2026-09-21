"""Send a file selected through an inline Rich Markdown button."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ...file_actions import (
    FILE_CALLBACK_PREFIX,
    RemoteFileReference,
    resolve_file_reference,
)
from ...transfer_runtime import get_node_runtime

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

    answered = False
    try:
        if isinstance(reference, Path):
            source: Any = reference.open("rb")
            filename = reference.name
        else:
            runtime = get_node_runtime(reference.node_id)
            if runtime is None:
                await query.answer("Файл больше недоступен", show_alert=True)
                return True
            result = await runtime.download_session_file(
                reference.node_id, reference.session_id, reference.path
            )
            if not result.get("ok", False):
                await query.answer("Файл больше недоступен", show_alert=True)
                return True
            source = io.BytesIO(bytes(result.get("content", b"")))
            filename = str(result.get("name", reference.name))
            source.name = filename
        await query.answer()
        answered = True
        with source:
            await context.bot.send_document(
                chat_id=user.id,
                document=source,
                filename=filename,
                disable_notification=True,
            )
    except Exception as exc:
        if isinstance(reference, RemoteFileReference):
            logger.warning(
                "remote file button send failed node=%s session=%s path=%s: %s",
                reference.node_id,
                reference.session_id,
                reference.path,
                exc,
            )
        else:
            logger.warning("file button send failed path=%s: %s", reference, exc)
        if not answered:
            await query.answer("Файл больше недоступен", show_alert=True)
            return True
        await context.bot.send_message(
            chat_id=user.id,
            text="Не удалось отправить файл.",
            disable_notification=True,
        )
    return True
