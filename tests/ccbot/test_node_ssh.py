from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ccbot.main import main
from ccbot.node_models import Node
from ccbot.node_ssh import build_ssh_command, main as ssh_main


def test_build_ssh_command_uses_worker_identity_and_private_bastion() -> None:
    node = Node(
        id="worker-a",
        display_name="Worker A",
        ssh_host="127.0.0.1",
        ssh_user="artem",
        ssh_port=22041,
        ssh_proxy_jump="ccbot-bastion",
    )

    command = build_ssh_command(node, ["sudo", "systemctl", "status", "ccbot"])

    assert command == [
        "ssh",
        "-J",
        "ccbot-bastion",
        "-p",
        "22041",
        "--",
        "artem@127.0.0.1",
        "sudo",
        "systemctl",
        "status",
        "ccbot",
    ]


def test_build_ssh_command_supports_direct_private_network_address() -> None:
    node = Node(
        id="worker-a",
        display_name="Worker A",
        ssh_host="10.42.0.8",
        ssh_user="worker",
    )

    assert build_ssh_command(node) == [
        "ssh",
        "-p",
        "22",
        "--",
        "worker@10.42.0.8",
    ]


def test_ssh_cli_executes_exact_argv_from_node_registry(
    tmp_path: Path, monkeypatch
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "nodes": {
                    "worker-a": Node(
                        id="worker-a",
                        display_name="Worker A",
                        ssh_host="127.0.0.1",
                        ssh_user="worker",
                        ssh_port=22041,
                        ssh_proxy_jump="bastion",
                    ).to_dict()
                }
            }
        ),
        encoding="utf-8",
    )
    executed: list[list[str]] = []
    monkeypatch.setattr(
        "ccbot.node_ssh.os.execvp", lambda _program, argv: executed.append(argv)
    )

    ssh_main(
        [
            "worker-a",
            "--state-file",
            str(state_file),
            "--",
            "id",
            "-u",
        ]
    )

    assert executed == [
        [
            "ssh",
            "-J",
            "bastion",
            "-p",
            "22041",
            "--",
            "worker@127.0.0.1",
            "id",
            "-u",
        ]
    ]


def test_ssh_cli_rejects_node_without_configured_endpoint(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"nodes": {"worker-a": Node("worker-a", "Worker A").to_dict()}}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="2"):
        ssh_main(["worker-a", "--state-file", str(state_file)])


def test_main_dispatches_node_ssh_without_telegram_configuration(monkeypatch) -> None:
    called: list[list[str]] = []
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_USERS", raising=False)
    monkeypatch.setattr("ccbot.node_ssh.main", lambda argv: called.append(argv))
    monkeypatch.setattr(
        sys,
        "argv",
        ["ccbot", "node", "ssh", "worker-a", "--", "hostname"],
    )

    main()

    assert called == [["worker-a", "--", "hostname"]]
