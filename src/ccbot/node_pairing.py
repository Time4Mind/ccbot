"""Copyable, short-lived invitation payloads for leader/worker pairing."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse


PAIRING_SCHEME = "ccbot-node"
DEFAULT_INVITATION_TTL = 10 * 60


@dataclass(frozen=True)
class PairingInvitation:
    relay_url: str
    leader_id: str
    nonce: str
    secret: str
    expires_at: float

    def to_link(self) -> str:
        """Serialize as a copy/paste-friendly URI."""
        return f"{PAIRING_SCHEME}://pair?{
            urlencode(
                {
                    'relay': self.relay_url,
                    'leader': self.leader_id,
                    'nonce': self.nonce,
                    'secret': self.secret,
                    'expires': str(int(self.expires_at)),
                }
            )
        }"

    @classmethod
    def from_link(cls, value: str, *, now: float | None = None) -> "PairingInvitation":
        parsed = urlparse(value.strip())
        if parsed.scheme != PAIRING_SCHEME or parsed.netloc != "pair":
            raise ValueError("invalid node pairing link")
        query = parse_qs(parsed.query, strict_parsing=True)

        def required(name: str) -> str:
            values = query.get(name)
            if not values or not values[0]:
                raise ValueError(f"pairing link misses {name}")
            return values[0]

        try:
            expires_at = float(required("expires"))
        except ValueError as exc:
            raise ValueError("pairing link has invalid expiry") from exc
        invitation = cls(
            relay_url=required("relay"),
            leader_id=required("leader"),
            nonce=required("nonce"),
            secret=required("secret"),
            expires_at=expires_at,
        )
        if invitation.is_expired(now=now):
            raise ValueError("pairing link has expired")
        return invitation

    def is_expired(self, *, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at


def create_pairing_invitation(
    *, relay_url: str, leader_id: str, ttl: int = DEFAULT_INVITATION_TTL
) -> PairingInvitation:
    if not relay_url.strip():
        raise ValueError("relay URL is required for node pairing")
    if not leader_id.strip():
        raise ValueError("leader id is required for node pairing")
    if ttl <= 0:
        raise ValueError("pairing TTL must be positive")
    now = time.time()
    return PairingInvitation(
        relay_url=relay_url.strip().rstrip("/"),
        leader_id=leader_id.strip(),
        nonce=secrets.token_urlsafe(12),
        secret=secrets.token_urlsafe(32),
        expires_at=float(int(now) + ttl),
    )


__all__ = [
    "DEFAULT_INVITATION_TTL",
    "PAIRING_SCHEME",
    "PairingInvitation",
    "create_pairing_invitation",
]
