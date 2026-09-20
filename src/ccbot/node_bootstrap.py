"""Direct, Telegram-free node bootstrap handle for automation agents."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .node_pairing import DEFAULT_INVITATION_TTL, create_pairing_invitation
from .utils import ccbot_dir


def _load_environment() -> None:
    """Load the same two dotenv locations as the main application."""
    local_env = Path(".env")
    global_env = ccbot_dir() / ".env"
    if local_env.is_file():
        load_dotenv(local_env)
    if global_env.is_file():
        load_dotenv(global_env)


def build_bootstrap_payload(
    *,
    relay_url: str,
    leader_id: str,
    signing_secret: str,
    node_id: str = "",
    display_name: str = "",
    ttl: int = DEFAULT_INVITATION_TTL,
) -> dict[str, Any]:
    """Build the safe machine-readable handoff for a remote worker agent."""
    if not relay_url.strip():
        raise ValueError("CCBOT_NODE_RELAY_URL is required")
    if not signing_secret.strip():
        raise ValueError("CCBOT_NODE_SECRET is required")
    resolved_node_id = node_id.strip() or f"worker-{secrets.token_hex(4)}"
    resolved_display_name = display_name.strip() or resolved_node_id
    invitation = create_pairing_invitation(
        relay_url=relay_url,
        leader_id=leader_id,
        node_id=resolved_node_id,
        ttl=ttl,
        signing_secret=signing_secret,
    )
    command_parts = [
        "uv",
        "run",
        "ccbot-node-agent",
        "--pairing",
        invitation.to_link(),
    ]
    command_parts.extend(("--node-id", resolved_node_id))
    command_parts.extend(("--name", resolved_display_name))
    command_parts.append("--install-service")
    return {
        "command": shlex.join(command_parts),
        "node_id": resolved_node_id,
        "display_name": resolved_display_name,
        "leader_id": invitation.leader_id,
        "relay_url": invitation.relay_url,
        "expires_at": int(invitation.expires_at),
    }


def main(argv: list[str] | None = None) -> None:
    """Print a one-time worker command without loading Telegram settings."""
    _load_environment()
    parser = argparse.ArgumentParser(
        prog="ccbot node bootstrap",
        description="Create a one-time command for an automation agent to join a node",
    )
    parser.add_argument(
        "--relay-url",
        default=os.environ.get("CCBOT_NODE_RELAY_URL", ""),
        help="leader relay host:port or tls://host:port",
    )
    parser.add_argument(
        "--leader-id",
        default=os.environ.get("CCBOT_NODE_LEADER_ID", "local") or "local",
    )
    parser.add_argument("--node-id", default="", help="stable worker id")
    parser.add_argument("--name", dest="display_name", default="")
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help=f"invitation lifetime in seconds (default: {DEFAULT_INVITATION_TTL})",
    )
    parser.add_argument(
        "--format",
        choices=("json", "command"),
        default="json",
        help="machine-readable JSON or the worker shell command",
    )
    args = parser.parse_args(argv)
    ttl = args.ttl
    if ttl is None:
        try:
            ttl = int(
                os.environ.get("CCBOT_NODE_PAIRING_TTL", str(DEFAULT_INVITATION_TTL))
            )
        except ValueError:
            parser.error("CCBOT_NODE_PAIRING_TTL must be an integer")
            return
    try:
        payload = build_bootstrap_payload(
            relay_url=args.relay_url,
            leader_id=args.leader_id,
            signing_secret=os.environ.get("CCBOT_NODE_SECRET", ""),
            node_id=args.node_id,
            display_name=args.display_name,
            ttl=ttl,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return
    if args.format == "command":
        print(payload["command"])
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


__all__ = ["build_bootstrap_payload", "main"]
