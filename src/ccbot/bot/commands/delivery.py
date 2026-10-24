"""Inspect pending final answers and explicitly reconcile lost acknowledgements."""

from __future__ import annotations

import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...handlers.message_sender import safe_reply
from .._common import is_user_allowed


async def delivery_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show delivery state without mistaking model completion for receipt."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return
    from ...final_delivery import final_delivery_queue

    pending = final_delivery_queue.pending(user.id)
    if not pending:
        await safe_reply(update.message, "Нет ответов, ожидающих доставки.")
        return
    for item in pending[:10]:
        unknown = item.status == "unknown"
        text = (
            "Telegram не подтвердил доставку ответа. Проверьте чат перед повтором."
            if unknown
            else "Ответ готов и сохранён. Доставка ожидает восстановления связи или открытия сессии."
        )
        text += f"\nСессия: {item.session_id}"
        keyboard = (
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Ответ получен", callback_data=f"delivery:ack:{item.id}"
                        ),
                        InlineKeyboardButton(
                            "Повторить отправку",
                            callback_data=f"delivery:retry:{item.id}",
                        ),
                    ]
                ]
            )
            if unknown
            else None
        )
        await safe_reply(update.message, text, reply_markup=keyboard)


async def delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Only an explicit user choice may replay an ambiguous new-message send."""
    user = update.effective_user
    query = update.callback_query
    if not user or not is_user_allowed(user.id) or query is None:
        return
    await query.answer()
    from ...final_delivery import final_delivery_queue

    parts = (query.data or "").split(":")
    if len(parts) != 3:
        return
    item = next(
        (r for r in final_delivery_queue.pending(user.id) if r.id == parts[2]), None
    )
    if item is None or item.status != "unknown":
        return
    if parts[1] == "ack":
        final_delivery_queue.resolve(item)
    elif parts[1] == "retry":
        item.status = "pending"
        item.retry_at = time.time()
        final_delivery_queue.save()
    else:
        return
    final_delivery_queue.kick()
    await query.edit_message_reply_markup(reply_markup=None)
