"""Atomic helpers for a card's Telegram carrier representation."""

from __future__ import annotations

from dataclasses import dataclass

from .card_types import CardState, CarrierKind

__all__ = [
    "CarrierBinding",
    "bind_carrier",
    "carrier_kind",
    "clear_carrier",
    "restore_carrier",
    "snapshot_carrier",
]


@dataclass(frozen=True)
class CarrierBinding:
    """Complete transport state tied to one Telegram message identity."""

    msg_id: int | None
    kind: CarrierKind = CarrierKind.TEXT
    rich_media_file_id: str = ""
    pane_hash: str = ""
    photo_edit_ts: float = 0.0


def carrier_kind(state: CardState) -> CarrierKind:
    """Return the normalized carrier kind, tolerating legacy test fixtures."""
    if state.is_rich_media_msg:
        return CarrierKind.RICH_MEDIA
    if state.is_photo_msg:
        return CarrierKind.LEGACY_PHOTO
    return CarrierKind.TEXT


def snapshot_carrier(state: CardState) -> CarrierBinding:
    """Capture all state that belongs to the current Telegram message."""
    return CarrierBinding(
        msg_id=state.msg_id,
        kind=carrier_kind(state),
        rich_media_file_id=state.rich_media_file_id,
        pane_hash=state.last_pane_hash,
        photo_edit_ts=state.last_photo_edit_ts,
    )


def restore_carrier(state: CardState, binding: CarrierBinding) -> None:
    """Atomically restore a previously captured carrier binding."""
    bind_carrier(
        state,
        binding.msg_id,
        binding.kind,
        rich_media_file_id=binding.rich_media_file_id,
        pane_hash=binding.pane_hash,
        photo_edit_ts=binding.photo_edit_ts,
    )


def bind_carrier(
    state: CardState,
    msg_id: int | None,
    kind: CarrierKind = CarrierKind.TEXT,
    *,
    rich_media_file_id: str = "",
    pane_hash: str = "",
    photo_edit_ts: float = 0.0,
) -> None:
    """Bind ``state`` to one message and set its transport kind as a unit."""
    state.msg_id = msg_id
    state.is_rich_media_msg = kind is CarrierKind.RICH_MEDIA
    state.is_photo_msg = kind is CarrierKind.LEGACY_PHOTO
    state.rich_media_file_id = (
        rich_media_file_id if kind is CarrierKind.RICH_MEDIA else ""
    )
    state.last_pane_hash = pane_hash if kind is not CarrierKind.TEXT else ""
    state.last_photo_edit_ts = photo_edit_ts if kind is not CarrierKind.TEXT else 0.0


def clear_carrier(state: CardState) -> int | None:
    """Release the Telegram message and clear every media-specific field."""
    old_msg_id = state.msg_id
    bind_carrier(state, None)
    return old_msg_id
