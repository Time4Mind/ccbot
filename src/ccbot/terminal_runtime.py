"""Node-scoped terminal access shared by cards and interactive UI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from .session import session_manager
from .session_models import Session
from .tmux_manager import tmux_manager
from .transfer_runtime import get_node_runtime


class PaneCaptureError(RuntimeError):
    """The session pane could not be obtained from its owning node."""


async def session_is_reachable(
    sess: Session, *, tmux: Any = None, runtime_getter: Any = None
) -> bool:
    """Check liveness only against the session's owning execution node."""
    tmux = tmux or tmux_manager
    runtime_getter = runtime_getter or get_node_runtime
    node_id = getattr(sess, "node_id", "local")
    if isinstance(node_id, str) and node_id != "local":
        runtime = runtime_getter(node_id)  # type: ignore[operator]
        routing_id = getattr(sess, "worker_session_id", "") or getattr(
            sess, "claude_session_id", ""
        )
        return runtime is not None and bool(routing_id)
    window_id = getattr(sess, "window_id", "")
    return bool(window_id and await tmux.find_window_by_id(window_id))  # type: ignore[attr-defined]


async def capture_session_pane(
    sess: Session,
    *,
    with_ansi: bool = False,
    tmux: Any = None,
    runtime_getter: Any = None,
) -> str:
    """Capture one session on its owner without leaking composite ids locally."""
    tmux = tmux or tmux_manager
    runtime_getter = runtime_getter or get_node_runtime
    node_id = getattr(sess, "node_id", "local")
    if isinstance(node_id, str) and node_id != "local":
        runtime = runtime_getter(node_id)  # type: ignore[operator]
        routing_id = getattr(sess, "worker_session_id", "") or getattr(
            sess, "claude_session_id", ""
        )
        if runtime is None:
            raise PaneCaptureError(f"Node is not connected: {node_id}")
        if not routing_id:
            raise PaneCaptureError("Remote session has no worker routing id")
        try:
            result = await runtime.capture_session(
                node_id, routing_id, with_ansi=with_ansi
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
        capture_many = getattr(tmux, "capture_panes", None)
        if callable(capture_many):
            capture_many_fn = cast(
                Callable[..., Awaitable[dict[str, str]]], capture_many
            )
            pane = (await capture_many_fn([sess.window_id], with_ansi=with_ansi)).get(
                sess.window_id
            )
        else:
            pane = await tmux.capture_pane(  # type: ignore[attr-defined]
                sess.window_id, with_ansi=with_ansi
            )
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise PaneCaptureError(detail) from exc
    if pane is None:
        raise PaneCaptureError("Local tmux pane is unavailable")
    return pane


async def send_session_key(sess: Session, key: str) -> bool:
    """Deliver one logical terminal key to the session's owning node."""
    node_id = getattr(sess, "node_id", "local")
    if isinstance(node_id, str) and node_id != "local":
        runtime = get_node_runtime(node_id)
        routing_id = sess.worker_session_id or sess.claude_session_id
        if runtime is None or not routing_id:
            return False
        try:
            result = await runtime.send_key(node_id, routing_id, key)
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


async def reset_session_binding(sess: Session) -> bool:
    """Forget provider identity after ``/clear`` without losing worker routing."""
    node_id = getattr(sess, "node_id", "local")
    if isinstance(node_id, str) and node_id != "local":
        runtime = get_node_runtime(node_id)
        routing_id = sess.worker_session_id or sess.claude_session_id
        if runtime is None or not routing_id:
            return False
        try:
            result = await runtime.reset_session_binding(node_id, routing_id)
        except Exception:
            return False
        if not result.get("ok", False):
            return False
        sess.claude_session_id = ""
        sess.provider_transcript_path = ""
    return True


async def capture_window_pane(
    window_id: str,
    *,
    with_ansi: bool = False,
    manager: Any = None,
    tmux: Any = None,
    runtime_getter: Any = None,
) -> str:
    """Capture a known session by owner, retaining orphan-local compatibility."""
    manager = manager or session_manager
    tmux = tmux or tmux_manager
    runtime_getter = runtime_getter or get_node_runtime
    sess = manager.find_session_by_window(window_id)  # type: ignore[attr-defined]
    session_window_id = getattr(sess, "window_id", "") if sess is not None else ""
    if isinstance(session_window_id, str) and session_window_id:
        return await capture_session_pane(
            cast(Session, sess),
            with_ansi=with_ansi,
            tmux=tmux,
            runtime_getter=runtime_getter,
        )
    window = await tmux.find_window_by_id(window_id)  # type: ignore[attr-defined]
    if window is None:
        raise PaneCaptureError("Session window is unavailable")
    pane = await tmux.capture_pane(  # type: ignore[attr-defined]
        window.window_id, with_ansi=with_ansi
    )
    if pane is None:
        raise PaneCaptureError("Local tmux pane is unavailable")
    return pane
