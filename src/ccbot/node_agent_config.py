"""Environment-derived public node-agent identity and connection receipts."""

from __future__ import annotations

import getpass
import os
from typing import Any, TypedDict


class WorkerCommands(TypedDict):
    claude_command: str
    codex_command: str
    zcode_command: str
    claude_flags: str
    codex_flags: str
    zcode_flags: str


def worker_command_options() -> WorkerCommands:
    """Resolve worker executables without changing another backend's command."""
    return {
        "claude_command": os.environ.get("CCBOT_CLAUDE_COMMAND", "claude"),
        "codex_command": os.environ.get("CCBOT_CODEX_COMMAND", "codex"),
        "zcode_command": os.environ.get(
            "CCBOT_ZCODE_COMMAND", os.environ.get("ZCODE_COMMAND", "zcode")
        ),
        "claude_flags": os.environ.get("CCBOT_CLAUDE_FLAGS", ""),
        "codex_flags": os.environ.get("CCBOT_CODEX_FLAGS", ""),
        "zcode_flags": os.environ.get("ZCODE_FLAGS", "--mode yolo --locale en-US"),
    }


def ssh_access_from_environment() -> dict[str, Any]:
    host = os.environ.get("CCBOT_NODE_SSH_HOST", "").strip()
    if not host:
        return {}
    user = os.environ.get("CCBOT_NODE_SSH_USER", "").strip() or getpass.getuser()
    try:
        port = int(os.environ.get("CCBOT_NODE_SSH_PORT", "22"))
    except ValueError as exc:
        raise RuntimeError("CCBOT_NODE_SSH_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("CCBOT_NODE_SSH_PORT must be between 1 and 65535")
    return {
        "host": host,
        "user": user,
        "port": port,
        "proxy_jump": os.environ.get("CCBOT_NODE_SSH_PROXY_JUMP", "").strip(),
    }


def print_connection_receipt(
    *, node_id: str, display_name: str, leader_id: str, relay_url: str
) -> None:
    print("ccbot-node-agent: connected", flush=True)
    print(f"node_id={node_id}", flush=True)
    print(f"display_name={display_name}", flush=True)
    print(f"leader_id={leader_id}", flush=True)
    print(f"relay_url={relay_url}", flush=True)
