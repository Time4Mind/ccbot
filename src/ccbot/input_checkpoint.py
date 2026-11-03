"""Repair delivery-proof checkpoints without replaying agent input."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .input_recovery import InputRecovery, InputRequest


def restore_checkpoint(
    recovery: InputRecovery, request: InputRequest, scan_positions: dict[str, int]
) -> bool:
    if not request.strict_codex_proof:
        return request.checkpoint is not None
    from .session import WindowState, session_manager
    from .codex_session_io import build_session_file_path

    state = session_manager.window_states.get(request.window_id)
    saved = request.checkpoint or {}
    if (
        recovery.source(request) is None
        or not isinstance(state, WindowState)
        or state.backend != "codex"
        or not state.session_id
    ):
        return bool(saved and not saved.get("unbound"))
    source = recovery.source(request)
    if source.claude_session_id and state.session_id != source.claude_session_id:
        return False
    path = (
        Path(state.transcript_path)
        if state.transcript_path
        else build_session_file_path(state.session_id, state.cwd)
    )
    if path is None or not path.is_file():
        return bool(saved and not saved.get("unbound"))
    try:
        stat = path.stat()
    except OSError:
        return False
    replaced = bool(
        saved.get("inode") is not None
        and (stat.st_ino != saved["inode"] or stat.st_dev != saved.get("device"))
    ) or stat.st_size < max(
        int(saved.get("offset", 0)), scan_positions.get(request.id, 0)
    )
    needs_binding = (
        not saved or saved.get("unbound") or saved.get("path") != str(path) or replaced
    )
    if needs_binding:
        # No byte checkpoint exists in this file. Never match an identical
        # historical user row, including the provisional/unbound startup path.
        if request.dispatch_started_at <= 0:
            return False
        request.checkpoint = {
            "path": str(path),
            "offset": 0,
            "backend": "codex",
            "not_before": request.dispatch_started_at,
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "replaced": replaced,
        }
        scan_positions.pop(request.id, None)
        recovery.save()
    return request.checkpoint is not None
