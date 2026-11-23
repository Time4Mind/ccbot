"""Explicit owner-initiated retries, independent of automatic recovery."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .input_recovery import InputRecovery, InputRequest


def remember(recovery: InputRecovery, request: InputRequest, status: str) -> None:
    """Keep only inputs that had a recovery action, even after a late receipt."""
    entry = recovery.retry_sources.setdefault(request.id, {})
    entry.update(
        status=status,
        sent_at=request.dispatch_started_at or request.received_at,
        settled_at=recovery.clock(),
        source={
            name: getattr(request, name)
            for name in (
                "id",
                "user_id",
                "message_id",
                "session_id",
                "window_id",
                "kind",
                "update",
                "prepared",
                "recognized",
                "preprocessed",
                "media_path",
                "received_at",
                "session_name",
                "sequence",
            )
        },
    )


def remember_outcome(
    recovery: InputRecovery, request: InputRequest, confirmed: bool
) -> None:
    if (
        request.state not in ("failed", "unknown")
        and request.id not in recovery.retry_sources
    ):
        return
    if recovery.retry_sources.get(request.id, {}).get("status") != "dismissed":
        remember(recovery, request, "confirmed" if confirmed else "failed")
    recovery.dirty.add((request.user_id, request.session_id))


def manual_retry(
    recovery: InputRecovery,
    user_id: int,
    message_id: str,
    command_id: int | str,
    application: Any,
) -> bool:
    from .input_composer_queue import enqueue_saved
    from .input_recovery import InputRequest
    from .session import session_manager
    from .opencode_tls_retry import tls_retries

    key = f"{user_id}:{message_id}"
    current = recovery.records.get(key)
    saved = recovery.retry_sources.get(key, {})
    if command_id in saved.get("actions", ()):
        return True
    if current is not None and current.state not in (
        "failed",
        "unknown",
        "awaiting_confirmation",
    ):
        return False
    if current is None and not saved:
        return False
    source_id = current.session_id if current else saved["source"]["session_id"]
    session = session_manager.get_session(source_id)
    if not session or session.state not in ("active", "idle") or not session.window_id:
        return False
    if current:
        remember(recovery, current, "retrying")
    entry = recovery.retry_sources[key]
    request = InputRequest(**entry["source"])
    request.window_id = session.window_id
    request.sequence = (
        max((r.sequence for r in recovery.records.values()), default=0) + 1
    )
    entry.setdefault("actions", []).append(command_id)
    entry["status"] = "retrying"
    recovery.records[key] = request
    tls_retries.admit(session, key)
    recovery.changed(request)
    enqueue_saved(request, application)
    return True
