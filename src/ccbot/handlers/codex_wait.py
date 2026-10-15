"""Dismiss informational Codex overlays without changing model or permissions."""

import asyncio
import logging

from ..codex_wait_prompt import parse_codex_wait_prompt
from ..session_models import Session
from ..terminal_runtime import PaneCaptureError, capture_session_pane, send_session_key

logger = logging.getLogger(__name__)
_MAX_ATTEMPTS = 3
_attempts: dict[tuple[str, str, str], tuple[str, int]] = {}
_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


async def dismiss_codex_wait_prompt(sess: Session | None, pane: str) -> bool:
    """Send at most one verified key per poll; True skips the stale snapshot.

    Selection is re-read before every key. Only option 2 can receive Enter.
    Failed/unmoving menus eventually fall through to the manual UI surface.
    No request retry, interrupt, model edit, or command approval happens here.
    """
    if sess is None or sess.backend != "codex":
        return False
    key = (sess.node_id, sess.id, sess.window_id)
    if parse_codex_wait_prompt(pane) is None:
        _attempts.pop(key, None)
        return False
    async with _locks.setdefault(key, asyncio.Lock()):
        try:
            fresh = parse_codex_wait_prompt(await capture_session_pane(sess))
        except PaneCaptureError:
            return False
        if fresh is None:
            _attempts.pop(key, None)
            return True  # Cached modal disappeared; do not act on its contents.
        previous, count = _attempts.get(key, ("", 0))
        count = count + 1 if previous == fresh.content else 1
        if count > _MAX_ATTEMPTS:
            return False
        _attempts[key] = (fresh.content, count)
        logical_key = {1: "Down", 2: "Enter", 3: "Up"}[fresh.selected]
        try:
            sent = await send_session_key(sess, logical_key)
        except Exception as exc:
            logger.debug("Codex wait-overlay key failed session=%s: %s", sess.id, exc)
            return False
        if sent:
            logger.info(
                "Codex slow-response overlay: key=%s attempt=%d session=%s node=%s",
                logical_key,
                count,
                sess.id,
                sess.node_id,
            )
            return True
        return False
