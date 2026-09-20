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
