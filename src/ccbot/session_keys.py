"""Canonical matching for hook-written tmux window keys."""

from .config import config


def key_matches_window(key: str, window_id: str) -> bool:
    """True if a session-map key targets window_id in our tmux server."""
    base = config.tmux_session_name
    suffix = f":{window_id}"
    if not key.endswith(suffix):
        return False
    prefix = key[: -len(suffix)]
    if prefix == base:
        return True
    grouped = f"{base}-w"
    if not prefix.startswith(grouped):
        return False
    tail = prefix[len(grouped) :]
    return bool(tail) and tail.isdigit()
