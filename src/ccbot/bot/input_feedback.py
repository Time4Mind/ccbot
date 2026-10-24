"""Coalesced voice-recovery state on the existing selected session card."""

from __future__ import annotations

from typing import Any

from telegram.error import BadRequest, NetworkError


async def refresh_request_status(recovery: Any, user_id: int, session_id: str) -> bool:
    from ..session import session_manager
    from ..handlers.notifications import get_card_state, refresh_panel
    from ..handlers.card_surface import surface_card_after_message
    from ..final_delivery import before_submission
    from ..telegram_rate_limit import background_telegram_request

    session = session_manager.get_session(session_id)
    active = session_manager.get_active_session(user_id)
    if session is None or active is None or active.id != session_id:
        return True
    state = get_card_state(user_id, session)
    if state.in_menu_view or state.in_kb_mode:
        return False
    pending = [
        r
        for r in recovery.records.values()
        if r.user_id == user_id and r.session_id == session_id
    ]
    work = [r for r in pending if r.state not in ("failed", "unknown")]
    state.voice_pending = any(r.kind == "voice" and not r.recognized for r in work)
    # The voice handler owns recognized receipts, including cached recognition
    # on restart. A transcript echo may already have consumed that receipt while
    # submission proof is still pending; recovery must not recreate it.
    latest = max(pending, key=lambda r: r.sequence) if pending else None
    if latest is not None and latest.ui_retry_at > recovery.clock():
        return False
    old_id = state.msg_id
    creates = (
        latest is not None
        and (old_id is None or old_id <= latest.message_id)
        and not latest.ui_unknown
    )
    if latest is not None and latest.ui_unknown and old_id is None:
        return True
    try:
        with background_telegram_request():
            if creates and latest is not None:
                rendered = await surface_card_after_message(
                    recovery.bot,
                    user_id,
                    session,
                    latest.message_id,
                    preserve_turn=True,
                )
            else:
                rendered = await refresh_panel(
                    recovery.bot, user_id, immediate=True, refresh_pane=False
                )
    except Exception as exc:
        if latest is not None:
            if (
                creates
                and isinstance(exc, (NetworkError, TimeoutError))
                and not isinstance(exc, BadRequest)
                and not before_submission(exc)
            ):
                latest.ui_unknown = True
            latest.ui_retry_at = recovery.clock() + recovery.retry_delay(
                latest.ui_attempts, exc
            )
            latest.ui_attempts += 1
            recovery.save()
        return False
    if rendered:
        return True
    if latest is not None:
        latest.ui_retry_at = recovery.clock() + recovery.retry_delay(
            latest.ui_attempts, RuntimeError()
        )
        latest.ui_attempts += 1
        recovery.save()
    return latest is None
