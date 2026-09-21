"""Node-scoped terminal access shared by cards and interactive UI."""

from __future__ import annotations

from .session_models import Session
from .tmux_manager import tmux_manager
from .transfer_runtime import get_node_runtime


class PaneCaptureError(RuntimeError):
    """The session pane could not be obtained from its owning node."""


async def capture_session_pane(sess: Session, *, with_ansi: bool = False) -> str:
    """Capture one session on its owner without leaking composite ids locally."""
    if sess.node_id != "local":
        runtime = get_node_runtime(sess.node_id)
        routing_id = sess.worker_session_id or sess.claude_session_id
        if runtime is None:
            raise PaneCaptureError(f"Node is not connected: {sess.node_id}")
        if not routing_id:
            raise PaneCaptureError("Remote session has no worker routing id")
        try:
            result = await runtime.capture_session(
                sess.node_id, routing_id, with_ansi=with_ansi
            )
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise PaneCaptureError(detail) from exc
        if not result.get("ok", False):
            detail = str(result.get("error", "")).strip()
            raise PaneCaptureError(detail or "Remote pane capture failed")
        return str(result.get("pane", ""))

    if not sess.window_id:
        raise PaneCaptureError("Local session has no tmux window")
    try:
        pane = (
            await tmux_manager.capture_panes([sess.window_id], with_ansi=with_ansi)
        ).get(sess.window_id)
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise PaneCaptureError(detail) from exc
    if pane is None:
        raise PaneCaptureError("Local tmux pane is unavailable")
    return pane


async def send_session_key(sess: Session, key: str) -> bool:
    """Deliver one logical terminal key to the session's owning node."""
    if sess.node_id != "local":
        runtime = get_node_runtime(sess.node_id)
        routing_id = sess.worker_session_id or sess.claude_session_id
        if runtime is None or not routing_id:
            return False
        try:
            result = await runtime.send_key(sess.node_id, routing_id, key)
        except Exception:
            return False
        return bool(result.get("ok"))
    if not sess.window_id:
        return False
    window = await tmux_manager.find_window_by_id(sess.window_id)
    if window is None:
        return False
    return await tmux_manager.send_keys(
        window.window_id, key, enter=False, literal=False
    )
