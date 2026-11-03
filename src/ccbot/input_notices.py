"""Human-readable input receipts always identify their original session."""

from __future__ import annotations
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .input_recovery import InputRequest


def session_name(request: InputRequest) -> str:
    from .session import session_manager

    source = session_manager.get_session(request.session_id)
    if source is not None and source.name:
        request.session_name = source.name
    return request.session_name or request.session_id


def notice_text(request: InputRequest) -> str:
    header = f"Сессия «{session_name(request)}» · запрос №{request.message_id}"
    message = request.update.get("message", {})
    original = message.get("text") or message.get("caption") or request.recognized
    preview = " ".join(str(original or "").split())
    if len(preview) > 160:
        preview = preview[:157] + "…"
    parts = [header]
    if preview:
        parts.append(f"Запрос: «{preview}»")
    parts.append(request.notice)
    if request.state == "unknown" and "/requests" not in request.notice:
        parts.append("Проверить состояние: /requests.")
    return "\n".join(parts)


def resolved_text(item: dict[str, Any]) -> str:
    from .session import session_manager

    source = session_manager.get_session(item.get("session_id", ""))
    name = (
        (source.name if source is not None else "")
        or item.get("session_name")
        or item.get("session_id")
        or "неизвестная сессия"
    )
    return f"Сессия «{name}» · запрос №{item['message_id']}: получение агентом подтверждено."
