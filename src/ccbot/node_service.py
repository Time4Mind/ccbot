"""Install the worker agent as a user launchd/systemd service."""

from __future__ import annotations

import os
import platform
import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _agent_executable() -> str:
    executable = shutil.which("ccbot-node-agent")
    if not executable:
        raise RuntimeError("ccbot-node-agent executable is not available")
    return executable


def _service_environment(
    *, relay_url: str, leader_id: str, credential_file: Path
) -> dict[str, str]:
    environment = {
        "CCBOT_NODE_RELAY_URL": relay_url,
        "CCBOT_NODE_LEADER_ID": leader_id,
        "CCBOT_NODE_CREDENTIAL_FILE": str(credential_file),
    }
    for key in (
        "CCBOT_WORKER_WORKDIR",
        "CCBOT_WORKER_TMUX_SESSION",
        "CCBOT_CLAUDE_COMMAND",
        "CCBOT_CODEX_COMMAND",
        "CCBOT_CLAUDE_FLAGS",
        "CCBOT_CODEX_FLAGS",
        "CCBOT_NODE_BACKENDS",
        "CCBOT_NODE_CONTEXT_DIR",
    ):
        if value := os.environ.get(key):
            environment[key] = value
    return environment


def install_node_service(
    *,
    node_id: str,
    display_name: str,
    relay_url: str,
    leader_id: str,
    credential_file: Path,
    runner: Runner = subprocess.run,
    system: str | None = None,
) -> Path:
    """Write, start and verify a user service containing no pairing secret."""
    executable = _agent_executable()
    command = [
        executable,
        "--node-id",
        node_id,
        "--name",
        display_name or node_id,
        "--credential-file",
        str(credential_file),
    ]
    environment = _service_environment(
        relay_url=relay_url,
        leader_id=leader_id,
        credential_file=credential_file,
    )
    system = system or platform.system()
    if system == "Linux":
        path = Path.home() / ".config/systemd/user/ccbot-node-agent.service"
        path.parent.mkdir(parents=True, exist_ok=True)
        env_lines = "\n".join(
            f"Environment={shlex.quote(f'{key}={value}')}"
            for key, value in sorted(environment.items())
        )
        path.write_text(
            "[Unit]\nDescription=ccbot remote node agent\n"
            "After=network-online.target\n\n[Service]\nType=simple\n"
            f"{env_lines}\nExecStart={shlex.join(command)}\nRestart=always\n"
            "RestartSec=2\n\n[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        runner(["systemctl", "--user", "daemon-reload"], check=True, text=True)
        runner(
            ["systemctl", "--user", "enable", "--now", path.name],
            check=True,
            text=True,
        )
        runner(
            ["systemctl", "--user", "is-active", "--quiet", path.name],
            check=True,
            text=True,
        )
        return path
    if system == "Darwin":
        label = "com.ccbot.node-agent"
        path = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": command,
                    "EnvironmentVariables": environment,
                    "RunAtLoad": True,
                    "KeepAlive": True,
                }
            )
        )
        path.chmod(0o600)
        domain = f"gui/{os.getuid()}"
        runner(
            ["launchctl", "bootout", domain, str(path)],
            check=False,
            text=True,
        )
        runner(["launchctl", "bootstrap", domain, str(path)], check=True, text=True)
        runner(["launchctl", "print", f"{domain}/{label}"], check=True, text=True)
        return path
    raise RuntimeError(f"unsupported supervisor platform: {system}")


__all__ = ["install_node_service"]
