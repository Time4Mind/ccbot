"""Single-token Telegram commands for durable input recovery."""

from __future__ import annotations

import re

REQUEST_ACTION = re.compile(
    r"^/requests_(retry|rtry|ack|cancel)_([1-9][0-9]*)(?:@([a-z0-9_]+))?$",
    re.IGNORECASE,
)
_LEGACY_ACTION = re.compile(r"/requests (retry|ack|cancel) ([1-9][0-9]*)\b")


def clickable_actions(text: str) -> str:
    """Render persisted legacy notices without changing their recovery state."""
    return _LEGACY_ACTION.sub(r"/requests_\1_\2", text)
