"""Repair delivery-proof checkpoints without replaying agent input."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .input_recovery import InputRecovery, InputRequest


def restore_checkpoint(
    recovery: InputRecovery, request: InputRequest, scan_positions: dict[str, int]
) -> bool:
    if request.strict_codex_proof and not (request.checkpoint or {}).get("unbound"):
        from .session import WindowState, session_manager
        from .codex_session_io import build_session_file_path

        source = recovery.source(request)
        state = session_manager.window_states.get(request.window_id)
        if (
            source is not None
            and isinstance(state, WindowState)
            and state.backend == "codex"
            and state.session_id
            and state.session_id == source.claude_session_id
        ):
            path = (
                Path(state.transcript_path)
                if state.transcript_path
                else build_session_file_path(state.session_id, state.cwd)
            )
            replaced = False
            if path is not None and path.is_file() and request.checkpoint:
                try:
                    current = path.stat()
                    saved_inode = request.checkpoint.get("inode")
                    replaced = (
                        saved_inode is not None
                        and (
                            current.st_ino != saved_inode
                            or current.st_dev != request.checkpoint.get("device")
                        )
                    ) or current.st_size < int(request.checkpoint.get("offset", 0))
                except OSError:
                    pass
            if (
                path is not None
                and path.is_file()
                and (
                    request.checkpoint is None
                    or request.checkpoint.get("path") != str(path)
                    or replaced
                )
            ):
                boundary = request.dispatch_started_at
                # An admission timestamp is too early when requests queue;
                # using it could match a different, identical user row.
                if boundary > 0:
                    request.checkpoint = {
                        "path": str(path),
                        "offset": 0,
                        "backend": "codex",
                        "not_before": boundary,
                        "device": path.stat().st_dev,
                        "inode": path.stat().st_ino,
                        "replaced": replaced,
                    }
                    scan_positions.pop(request.id, None)
                    recovery.save()
    if request.checkpoint is None:
        return False
    if request.checkpoint.get("unbound"):
        from .session import WindowState, session_manager
        from .codex_session_io import build_session_file_path

        if recovery.source(request) is None:
            return False
        state = session_manager.window_states.get(request.window_id)
        if (
            not isinstance(state, WindowState)
            or not state.session_id
            or state.backend != "codex"
        ):
            return False
        path = (
            Path(state.transcript_path)
            if state.transcript_path
            else build_session_file_path(state.session_id, state.cwd)
        )
        if path is None or not path.is_file():
            return False
        request.checkpoint = {"path": str(path), "offset": 0, "backend": "codex"}
        recovery.save()
    return True
