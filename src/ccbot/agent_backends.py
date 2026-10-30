"""Resolve a transcript using its recorded agent backend, never the current default."""

from pathlib import Path


def session_transcript_path(session_id: str, cwd: str, backend: str) -> Path | None:
    if backend == "zcode":
        from .zcode_session_io import build_session_file_path
    elif backend == "codex":
        from .codex_session_io import build_session_file_path
    elif backend == "claude":
        from .session_claude_io import build_session_file_path
    else:
        raise ValueError(f"Unsupported agent backend: {backend}")
    return build_session_file_path(session_id, cwd)
