"""Restore session status at startup without delaying context enrichment."""

from __future__ import annotations

import logging
from typing import Any

from ..config import config
from ..session import session_manager

logger = logging.getLogger(__name__)


async def seed_lifecycle_statuses() -> bool:
    """Reconcile persisted badges from transcript tails before card restore."""
    from ..handlers import bg_status

    changed = False
    for user_id in config.allowed_users:
        for sess in list(session_manager.sessions.values()):
            if sess.state not in ("active", "idle"):
                continue
            try:
                inferred = await bg_status.infer_status_from_jsonl(sess)
            except Exception as exc:
                logger.debug("infer bg status failed for %s: %s", sess.id, exc)
                continue
            current = bg_status.get_status(user_id, sess.id)
            if inferred == "working":
                seed_status: bg_status.Status = "working"
            elif current in ("finished", "seen_finished", "error"):
                seed_status = current
            elif current is not None:
                seed_status = "finished"
            else:
                seed_status = "seen_finished"
            changed = bg_status.update_status(user_id, sess.id, seed_status) or changed
    if changed:
        session_manager.save_state()
    return changed


async def seed_bg_context(application: Any) -> None:
    """Populate context percentages off the startup-critical path."""
    from ..handlers import bg_status
    from ..handlers.card_terminal import sync_card_identity
    from ..handlers.notifications import get_card_state, refresh_panel
    from ..usage import context_pct_for_session

    for user_id in config.allowed_users:
        changed = False
        active = session_manager.get_active_session(user_id)
        if active is not None:
            await sync_card_identity(active, get_card_state(user_id, active))
            await refresh_panel(application.bot, user_id)
        sessions = list(session_manager.sessions.values())
        if active is not None:
            sessions.sort(key=lambda sess: sess.id != active.id)
        for sess in sessions:
            if sess.state not in ("active", "idle"):
                continue
            try:
                pct = await context_pct_for_session(sess)
            except Exception as exc:
                logger.debug("infer bg context failed for %s: %s", sess.id, exc)
                continue
            if pct is not None:
                bg_status.set_context_pct(user_id, sess.id, pct)
                changed = True
                if active is not None and sess.id == active.id:
                    await refresh_panel(application.bot, user_id)
        if changed:
            try:
                await refresh_panel(application.bot, user_id)
            except Exception as exc:
                logger.debug("refresh_panel after context seed failed: %s", exc)
