from __future__ import annotations

import json
import shlex
import sys

import pytest

from ccbot.main import main
from ccbot.node_bootstrap import build_bootstrap_payload
from ccbot.node_pairing import PairingInvitation


def test_bootstrap_payload_is_machine_readable_and_contains_worker_command() -> None:
    payload = build_bootstrap_payload(
        relay_url="relay.example.test:8765",
        leader_id="local",
        signing_secret="leader-secret",
        node_id="worker1",
        display_name="Worker 1",
        ttl=600,
    )

    assert payload["node_id"] == "worker1"
    assert payload["display_name"] == "Worker 1"
    assert payload["leader_id"] == "local"
    assert payload["relay_url"] == "relay.example.test:8765"
    command = shlex.split(payload["command"])
    assert command[:3] == ["uv", "run", "ccbot-node-agent"]
    assert "--install-service" in command
    invitation = PairingInvitation.from_link(command[command.index("--pairing") + 1])
    assert invitation.leader_id == "local"
    assert invitation.relay_url == "relay.example.test:8765"
    assert invitation.node_id == "worker1"


def test_bootstrap_assigns_and_binds_generated_node_id() -> None:
    payload = build_bootstrap_payload(
        relay_url="relay.example.test:8765",
        leader_id="local",
        signing_secret="leader-secret",
    )

    assert payload["node_id"].startswith("worker-")
    command = shlex.split(payload["command"])
    assert command[command.index("--node-id") + 1] == payload["node_id"]
    invitation = PairingInvitation.from_link(command[command.index("--pairing") + 1])
    assert invitation.node_id == payload["node_id"]


def test_bootstrap_can_persist_self_hosted_ssh_route_in_worker_service() -> None:
    payload = build_bootstrap_payload(
        relay_url="relay.example.test:8765",
        leader_id="local",
        signing_secret="leader-secret",
        node_id="worker1",
        ssh_host="127.0.0.1",
        ssh_user="worker",
        ssh_port=22041,
        ssh_proxy_jump="ccbot-bastion",
    )

    command = shlex.split(payload["command"])
    assert command[:9] == [
        "env",
        "CCBOT_NODE_SSH_HOST=127.0.0.1",
        "CCBOT_NODE_SSH_USER=worker",
        "CCBOT_NODE_SSH_PORT=22041",
        "CCBOT_NODE_SSH_PROXY_JUMP=ccbot-bastion",
        "uv",
        "run",
        "ccbot-node-agent",
        "--pairing",
    ]
    assert payload["ssh"] == {
        "host": "127.0.0.1",
        "user": "worker",
        "port": 22041,
        "proxy_jump": "ccbot-bastion",
    }


def test_ccbot_node_bootstrap_does_not_require_telegram_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_USERS", raising=False)
    monkeypatch.setenv("CCBOT_NODE_RELAY_URL", "relay.example.test:8765")
    monkeypatch.setenv("CCBOT_NODE_SECRET", "leader-secret")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ccbot",
            "node",
            "bootstrap",
            "--node-id",
            "worker1",
            "--name",
            "Worker 1",
        ],
    )

    main()

    output = json.loads(capsys.readouterr().out)
    assert output["node_id"] == "worker1"
    assert output["display_name"] == "Worker 1"
    assert "--pairing" in output["command"]
    assert "leader-secret" not in output["command"]


def test_bootstrap_requires_relay_and_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CCBOT_NODE_RELAY_URL", raising=False)
    monkeypatch.delenv("CCBOT_NODE_SECRET", raising=False)
    monkeypatch.setattr(sys, "argv", ["ccbot", "node", "bootstrap"])

    with pytest.raises(SystemExit, match="2"):
        main()

    assert "CCBOT_NODE_RELAY_URL" in capsys.readouterr().err
