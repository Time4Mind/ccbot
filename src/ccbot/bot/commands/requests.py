"""Inspect recoverable inputs and explicitly settle uncertain agent submission."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from .._common import is_user_allowed
from ...handlers.message_sender import safe_reply


async def requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not is_user_allowed(user.id) or update.message is None:
        return
    from ...input_recovery import input_recovery

    input_recovery.load()
    args = context.args or []
    if len(args) == 2 and args[0] == "retry":
        from ...input_media import retry_deferred_media

        item = input_recovery.records.get(f"{user.id}:{args[1]}")
        retried = item is not None and retry_deferred_media(
            input_recovery, item, context.application
        )
        await safe_reply(
            update.message,
            "Голосовое поставлено на повтор в исходную сессию."
            if retried
            else "Нет сохранённого голосового для повтора в доступную исходную сессию.",
        )
        return
    if len(args) == 2 and args[0] in ("ack", "cancel"):
        item = input_recovery.records.get(f"{user.id}:{args[1]}")
        if (
            item is not None
            and item.user_id == user.id
            and item.state in ("unknown", "failed")
        ):
            if args[0] == "ack":
                input_recovery.complete(item, confirmed=item.state == "unknown")
                await safe_reply(
                    update.message,
                    "Подтверждение принято. Запрос не будет отправлен повторно.",
                )
            else:
                input_recovery.fail(
                    item,
                    "Неопределённая передача отменена пользователем; автоповтора не будет.",
                )
                await safe_reply(
                    update.message, "Ожидание отменено. Автоповтора не будет."
                )
            return
    pending = [r for r in input_recovery.records.values() if r.user_id == user.id]
    if not pending:
        await safe_reply(update.message, "Нет запросов, ожидающих восстановления.")
        return
    labels = {
        "accepted": "принят, ожидает обработки",
        "downloading": "загружается файл",
        "transcribing": "распознаётся",
        "prepared": "подготовлен к передаче",
        "dispatching": "передаётся",
        "awaiting_confirmation": "ожидает подтверждения от агента",
        "unknown": "передача не подтверждена",
        "failed": "обработка завершилась ошибкой",
    }
    lines = []
    for item in pending[:20]:
        from ...input_notices import session_name

        line = f"№{item.message_id}, сессия «{session_name(item)}»: {labels.get(item.state, item.state)}"
        if item.notice:
            line += "\n" + item.notice
        if item.state == "unknown":
            line += f"\nПосле проверки сессии: /requests ack {item.message_id} или /requests cancel {item.message_id}"
        elif item.state == "failed" and item.notice_unknown:
            line += f"\nПосле прочтения ошибки: /requests ack {item.message_id}"
        lines.append(line)
    await safe_reply(update.message, "\n\n".join(lines))
