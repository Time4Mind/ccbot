"""Durable admission and recovery of never-submitted archive inputs."""

from __future__ import annotations
from typing import Any


def admit_entry(entry: Any, session: Any) -> Any:
    from .input_recovery import input_recovery

    message = entry.update.message
    if message is None:
        return None
    kind = (
        "voice"
        if message.voice
        else "photo"
        if message.photo
        else "document"
        if message.document
        else "text"
    )
    request = input_recovery.admit(entry.update, kind, session)
    if request is not None and request.state == "accepted":
        request.state = "waiting_session"
        request.startup_deferred = True
        input_recovery.changed(request)
    return request


def defer_session(session_id: str, window_id: str | None = None) -> None:
    from .input_recovery import input_recovery

    input_recovery.load()
    for request in list(input_recovery.records.values()):
        if (
            request.session_id != session_id
            or window_id is not None
            and request.window_id not in (window_id, "")
            or request.dispatch_started_at
            or request.state
            in ("unknown", "dispatching", "awaiting_confirmation", "delivered")
            or request.media_deferred
        ):
            continue
        request.startup_deferred = True
        input_recovery.fail(
            request,
            "Сессия не восстановилась. Запрос сохранён и не отправлен агенту. "
            "После восстановления исходной сессии очередь продолжит обработку. "
            f"Проверить: /requests; повторить вручную: /requests_retry_{request.message_id}",
        )


async def replay_entry(entry: Any, window_id: str, fallback: Any) -> bool:
    from .input_recovery import input_recovery
    from .session import session_manager
    from .bot import inbound
    from .inbound_queue import enqueue_inbound

    session = session_manager.find_session_by_window(window_id)
    if session is None:
        return await fallback(entry, window_id)
    request = admit_entry(entry, session)
    if request is None:
        if input_recovery.duplicate(entry.update):
            return True
        return await fallback(entry, window_id)
    if request.dispatch_started_at or request.state in (
        "dispatching",
        "unknown",
        "awaiting_confirmation",
        "delivered",
    ):
        return request.confirmed
    request.window_id = window_id
    request.state = "accepted"
    request.startup_deferred = False
    input_recovery.changed(request)
    kind = (
        "command"
        if entry.update.message.text and entry.update.message.text.startswith("/")
        else request.kind
    )
    receipt = enqueue_inbound(
        request.user_id,
        window_id,
        entry.update,
        entry.context,
        kind=request.kind,
        processor=getattr(inbound, "_run_" + kind),
        recovery_id=request.id,
    )
    return await receipt.completion


def resume_deferred(session: Any, context: Any, entries: Any) -> None:
    """Enqueue old, definitely unsent inputs before this restore's new inputs."""
    from .input_recovery import input_recovery
    from .input_media import retry_deferred_media

    application = getattr(context, "application", None)
    if application is None:
        return
    current_ids = {input_recovery.key(e.update) for e in entries}
    for request in sorted(
        list(input_recovery.records.values()), key=lambda r: r.sequence
    ):
        if (
            request.session_id == session.id
            and request.startup_deferred
            and request.id not in current_ids
        ):
            if request.state == "waiting_session":
                request.state = "failed"
            retry_deferred_media(input_recovery, request, application)


async def notify_restore_failure(
    bot: Any, user_id: int, session: Any, error: Any
) -> None:
    from .handlers.message_sender import safe_send

    await safe_send(
        bot,
        user_id,
        f"Не удалось восстановить сессию «{getattr(session, 'name', '') or session.id}». "
        f"Причина: {error}.\n"
        "Непереданные запросы сохранены в /requests. Другие сессии доступны.",
        disable_notification=False,
    )


def rollback_selection(
    manager: Any,
    user_id: int,
    failed_id: str,
    node_id: str,
    prior_active: str | None,
    prior_node: str | None,
    history: list[str],
) -> None:
    """A late startup failure must not undo a newer session selection."""
    if manager.active_sessions.get(user_id) == failed_id:
        if prior_active is None:
            manager.active_sessions.pop(user_id, None)
        else:
            manager.active_sessions[user_id] = prior_active
        if history:
            manager.active_history[user_id] = history
        else:
            manager.active_history.pop(user_id, None)
    nodes = manager.active_sessions_by_node.get(user_id, {})
    if nodes.get(node_id) == failed_id:
        if prior_node is None:
            nodes.pop(node_id, None)
        else:
            nodes[node_id] = prior_node
        if not nodes:
            manager.active_sessions_by_node.pop(user_id, None)
