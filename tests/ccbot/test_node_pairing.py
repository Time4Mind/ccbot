from __future__ import annotations

import pytest

from ccbot.node_pairing import PairingInvitation, create_pairing_invitation


def test_pairing_invitation_is_copyable_and_round_trips() -> None:
    invitation = create_pairing_invitation(
        relay_url="https://relay.example.test/ccbot",
        leader_id="leader-1",
        ttl=600,
    )

    restored = PairingInvitation.from_link(
        invitation.to_link(), now=invitation.expires_at - 1
    )

    assert restored == invitation
    assert invitation.to_link().startswith("ccbot-node://pair?")


def test_pairing_invitation_rejects_expired_link() -> None:
    invitation = create_pairing_invitation(
        relay_url="https://relay.example.test",
        leader_id="leader-1",
        ttl=1,
    )

    with pytest.raises(ValueError, match="expired"):
        PairingInvitation.from_link(invitation.to_link(), now=invitation.expires_at)


def test_pairing_requires_relay_and_leader() -> None:
    with pytest.raises(ValueError, match="relay URL"):
        create_pairing_invitation(relay_url="", leader_id="leader")
    with pytest.raises(ValueError, match="leader id"):
        create_pairing_invitation(relay_url="https://relay", leader_id="")
