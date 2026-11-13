"""Resolve a worker's native history without crossing backend identities."""

from pathlib import Path

from .agent_backends import session_transcript_path


def restore_transcript_path(
    session_id: str, workdir: str, backend: str, provider_transcript_path: str = ""
) -> Path:
    if provider_transcript_path:
        exact = Path(provider_transcript_path).expanduser()
        if exact.is_file():
            if backend == "codex":
                from .codex_session_io import session_id_for_path
            elif backend == "zcode":
                from .zcode_session_io import session_id_for_path
            elif backend == "opencode":
                from .opencode_session_io import session_id_for_path
            elif backend == "claude":
                if exact.stem != session_id:
                    raise ValueError(
                        "Persisted transcript path does not match provider session id"
                    )
                return exact
            else:
                raise ValueError(f"unsupported source backend: {backend}")
            if session_id_for_path(exact) != session_id:
                raise ValueError(
                    "Persisted transcript path does not match provider session id"
                )
            return exact
    path = session_transcript_path(session_id, workdir, backend)
    if path is None or not path.is_file():
        label = (
            "Codex rollout"
            if backend == "codex"
            else f"{backend.capitalize()} transcript"
        )
        raise ValueError(f"{label} not found on worker")
    return path
