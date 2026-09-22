"""Synchronize node-owned terminal identity into the shared card model."""

from __future__ import annotations

import logging

from ..session_models import Session
from ..terminal_parser import parse_codex_model_effort
from ..terminal_runtime import PaneCaptureError, capture_session_pane
from .card_types import CardState

logger = logging.getLogger(__name__)


async def sync_card_identity(sess: Session, state: CardState) -> bool:
    """Refresh Codex model/effort from the session's owning node."""
    if sess.backend != "codex" or not sess.window_id:
        return False
    try:
        pane = await capture_session_pane(sess)
    except PaneCaptureError as exc:
        logger.debug("Card identity capture failed session=%s: %s", sess.id, exc)
        return False
    identity = parse_codex_model_effort(pane)
    if identity is None:
        return False
    model, effort = identity
    changed = state.agent_model != model or state.reasoning_effort != effort
    state.agent_model = model
    state.reasoning_effort = effort
    return changed
