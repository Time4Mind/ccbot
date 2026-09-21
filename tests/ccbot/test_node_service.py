from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ccbot.node_service import install_node_service


def test_linux_service_is_started_verified_and_contains_no_pairing_secret(
    tmp_path, monkeypatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "ccbot.node_service._agent_executable", lambda: "/opt/ccbot-node-agent"
    )

    def runner(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    path = install_node_service(
        node_id="worker-a",
        display_name="Worker A",
        relay_url="tls://relay:8765",
        leader_id="leader",
        credential_file=tmp_path / "worker/credential.json",
        runner=runner,
        system="Linux",
    )

    content = path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600
    assert "--pairing" not in content
    assert "worker-secret" not in content
    assert "CCBOT_NODE_RELAY_URL=tls://relay:8765" in content
    assert calls[-1][:3] == ["systemctl", "--user", "is-active"]


def test_service_preserves_non_secret_ssh_endpoint_configuration(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "ccbot.node_service._agent_executable", lambda: "/opt/ccbot-node-agent"
    )
    monkeypatch.setenv("CCBOT_NODE_SSH_HOST", "127.0.0.1")
    monkeypatch.setenv("CCBOT_NODE_SSH_USER", "artem")
    monkeypatch.setenv("CCBOT_NODE_SSH_PORT", "22041")
    monkeypatch.setenv("CCBOT_NODE_SSH_PROXY_JUMP", "bastion")

    path = install_node_service(
        node_id="worker-a",
        display_name="Worker A",
        relay_url="tls://relay:8765",
        leader_id="leader",
        credential_file=tmp_path / "worker/credential.json",
        runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
        system="Linux",
    )

    content = path.read_text(encoding="utf-8")
    assert "CCBOT_NODE_SSH_HOST=127.0.0.1" in content
    assert "CCBOT_NODE_SSH_USER=artem" in content
    assert "CCBOT_NODE_SSH_PORT=22041" in content
    assert "CCBOT_NODE_SSH_PROXY_JUMP=bastion" in content


def test_service_preserves_provider_transcript_environment(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "ccbot.node_service._agent_executable", lambda: "/opt/ccbot-node-agent"
    )
    monkeypatch.setenv("CODEX_HOME", "/srv/codex-current")
    monkeypatch.setenv("CCBOT_CODEX_SESSIONS_PATH", "/srv/codex-current/sessions")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/srv/claude-current")
    monkeypatch.setenv("CCBOT_CLAUDE_PROJECTS_PATH", "/srv/claude-current/projects")

    path = install_node_service(
        node_id="worker-a",
        display_name="Worker A",
        relay_url="tls://relay:8765",
        leader_id="leader",
        credential_file=tmp_path / "worker/credential.json",
        runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
        system="Linux",
    )

    content = path.read_text(encoding="utf-8")
    assert "CODEX_HOME=/srv/codex-current" in content
    assert "CCBOT_CODEX_SESSIONS_PATH=/srv/codex-current/sessions" in content
    assert "CLAUDE_CONFIG_DIR=/srv/claude-current" in content
    assert "CCBOT_CLAUDE_PROJECTS_PATH=/srv/claude-current/projects" in content


def test_service_preserves_worker_backend_configuration(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "ccbot.node_service._agent_executable", lambda: "/opt/ccbot-node-agent"
    )
    monkeypatch.setenv("CCBOT_NODE_BACKENDS", "codex")

    path = install_node_service(
        node_id="worker-a",
        display_name="Worker A",
        relay_url="tls://relay:8765",
        leader_id="leader",
        credential_file=tmp_path / "worker/credential.json",
        runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
        system="Linux",
    )

    assert "CCBOT_NODE_BACKENDS=codex" in path.read_text(encoding="utf-8")
