"""Synchronize node-owned terminal identity into the shared card model."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import codex_session_io
from ..session import session_manager
from ..session_models import Session
from ..terminal_parser import parse_codex_fast_mode, parse_codex_model_effort
from ..terminal_runtime import PaneCaptureError, capture_session_pane
from .card_types import CardState

logger = logging.getLogger(__name__)


def apply_card_identity(
    state: CardState, identity: tuple[str, str], fast_mode: bool | None = None
) -> bool:
    """Apply known identity fields, preserving Fast when the pane omits it."""
    model, effort = identity
    changed = state.agent_model != model or state.reasoning_effort != effort
    if fast_mode is not None:
        changed = changed or state.fast_mode != fast_mode
        state.fast_mode = fast_mode
    state.agent_model = model
    state.reasoning_effort = effort
    return changed


async def sync_card_identity(sess: Session, state: CardState) -> bool:
    """Refresh Codex model/effort from the session's owning node."""
    if sess.backend != "codex" or not sess.window_id:
        return False
    identity = None
    fast_mode = None
    try:
        pane = await capture_session_pane(sess)
    except PaneCaptureError as exc:
        logger.debug("Card identity capture failed session=%s: %s", sess.id, exc)
    else:
        identity = parse_codex_model_effort(pane)
        fast_mode = parse_codex_fast_mode(pane)
    if identity is None and sess.node_id == "local" and sess.claude_session_id:
        window_state = session_manager.window_states.get(sess.window_id)
        path = (
            Path(window_state.transcript_path)
            if window_state and window_state.transcript_path
            else None
        )
        if path is None or not path.is_file():
            path = await asyncio.to_thread(
                codex_session_io.build_session_file_path,
                sess.claude_session_id,
                sess.workdir,
            )
        if path is not None:
            identity = await codex_session_io.model_effort(path)
    if identity is None:
        return False
    return apply_card_identity(state, identity, fast_mode)
