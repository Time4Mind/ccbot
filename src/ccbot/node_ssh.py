"""Open a standard SSH session to a registered worker node."""

from __future__ import annotations

import argparse
import json
import os
import shlex
from pathlib import Path

from .node_models import Node
from .utils import ccbot_dir


def build_ssh_command(node: Node, remote_command: list[str] | None = None) -> list[str]:
    """Build argv without a shell; SSH config/agent owns all credentials."""
    host = node.ssh_host.strip()
    user = node.ssh_user.strip()
    if not host or not user:
        raise ValueError(f"node {node.id} does not advertise SSH access")
    if any(char in host + user for char in "\r\n\0"):
        raise ValueError("SSH host and user must not contain control characters")
    if not 1 <= node.ssh_port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    command = ["ssh"]
    if node.ssh_proxy_jump:
        if any(char in node.ssh_proxy_jump for char in "\r\n\0"):
            raise ValueError("SSH proxy jump must not contain control characters")
        command.extend(("-J", node.ssh_proxy_jump))
    command.extend(("-p", str(node.ssh_port), "--", f"{user}@{host}"))
    command.extend(remote_command or ())
    return command


def _load_node(state_file: Path, node_id: str) -> Node:
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"cannot read node registry: {state_file}") from exc
    raw = state.get("nodes", {}) if isinstance(state, dict) else {}
    data = raw.get(node_id) if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        raise ValueError(f"unknown node: {node_id}")
    return Node.from_dict(data)


def main(argv: list[str] | None = None) -> None:
    """Replace this process with OpenSSH for interactive agent access."""
    raw_args = list(argv or [])
    remote_command: list[str] = []
    if "--" in raw_args:
        separator = raw_args.index("--")
        remote_command = raw_args[separator + 1 :]
        raw_args = raw_args[:separator]
    parser = argparse.ArgumentParser(
        prog="ccbot node ssh",
        description="Connect to a worker using its registered private SSH route",
    )
    parser.add_argument("node_id")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=ccbot_dir() / "state.json",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--print-command", action="store_true")
    args = parser.parse_args(raw_args)
    try:
        command = build_ssh_command(
            _load_node(args.state_file.expanduser(), args.node_id), remote_command
        )
    except ValueError as exc:
        parser.error(str(exc))
        return
    if args.print_command:
        print(shlex.join(command))
        return
    os.execvp(command[0], command)


__all__ = ["build_ssh_command", "main"]
