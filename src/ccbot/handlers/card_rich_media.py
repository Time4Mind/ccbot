"""Rich Markdown transport for live cards with an inline pane image."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardMarkup, Message
from telegram.error import BadRequest, RetryAfter

from .. import rich
from ..config import config
from ..session import session_manager
from .card_model import CardState
from .card_registry import lookup_session_for_message
from .kb_mode import _capture_pane_png

logger = logging.getLogger(__name__)

# Keep the same visible gap used between card events. Telegram collapses an
# empty ``<p><br></p>`` around media, while a non-breaking-space paragraph is
# preserved as its own rich block by every transport path.
_MEDIA_SPACER = "\u00a0"


@dataclass(frozen=True)
class RichCardSend:
    """A sent rich-media carrier and its reusable Telegram photo id."""

    message: Message
    photo_file_id: str


async def send_rich_media_card(
    bot: Any,
    user_id: int,
    state: CardState,
    text: str,
    pane_png: bytes,
    *,
    reply_markup: InlineKeyboardMarkup | None,
) -> RichCardSend | None:
    """Send a rich card ending in ``pane_png``; None requests legacy fallback."""
    if not config.rich_messages:
        return None
    try:
        message = await rich.send_rich_message(
            bot,
            user_id,
            _rich_card_markdown(text, state),
            reply_markup=reply_markup,
            photo=pane_png,
            disable_notification=True,
        )
    except RetryAfter:
        raise
    except Exception as exc:
        logger.warning("rich-media card send failed chat=%s: %s", user_id, exc)
        return None
    return RichCardSend(
        message=message,
        photo_file_id=rich.extract_rich_photo_file_id(message) or "",
    )


async def edit_rich_media_card(
    bot: Any,
    user_id: int,
    state: CardState,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    min_photo_interval: float,
    refresh_pane: bool = True,
) -> bool:
    """Edit text and keep the terminal screenshot as the final rich block."""
    if state.msg_id is None:
        return False

    sess_id = lookup_session_for_message(user_id, state.msg_id)
    sess = session_manager.get_session(sess_id) if sess_id else None
    window_id = sess.window_id if sess is not None else ""
    elapsed = time.monotonic() - state.last_photo_edit_ts

    photo: bytes | str | None = state.rich_media_file_id or None
    pane_hash = state.last_pane_hash
    uploaded_new_pane = False
    if window_id and (photo is None or refresh_pane and elapsed >= min_photo_interval):
        png, captured_hash = await _capture_pane_png(window_id)
        if png is not None and captured_hash and (
            photo is None or captured_hash != state.last_pane_hash
        ):
            photo = png
            pane_hash = captured_hash
            uploaded_new_pane = True

    if photo is None:
        logger.warning("rich-media card has no reusable pane photo msg=%s", state.msg_id)
        state.msg_id = None
        return False

    try:
        result = await rich.edit_rich_message(
            bot,
            user_id,
            state.msg_id,
            _rich_card_markdown(text, state),
            reply_markup=reply_markup,
            photo=photo,
        )
    except RetryAfter:
        raise
    except BadRequest as exc:
        error = str(exc)
        if "message is not modified" in error.lower():
            return True
        if _is_lost_carrier(error):
            logger.info("rich-media card lost carrier msg=%s err=%s", state.msg_id, error)
            state.msg_id = None
            return False
        logger.warning("rich-media card edit failed msg=%s: %s", state.msg_id, error)
        return False
    except Exception as exc:
        logger.warning("rich-media card edit failed msg=%s: %s", state.msg_id, exc)
        return False

    if uploaded_new_pane:
        new_file_id = rich.extract_rich_photo_file_id(result)
        # If a local Bot API proxy returned only True, do not reuse the old
        # id on the next text edit: that would restore the previous image.
        state.rich_media_file_id = new_file_id or ""
        state.last_pane_hash = pane_hash
        state.last_photo_edit_ts = time.monotonic()
    return True


def _rich_card_markdown(text: str, state: CardState) -> str:
    """Insert the spaced photo before context/background service metadata."""
    offset = state.media_anchor_offset
    if offset <= 0 or offset > len(text):
        return rich.to_rich_markdown(text)
    body = rich.to_rich_markdown(text[:offset]).rstrip()
    service_tail = rich.to_rich_markdown(text[offset:].lstrip()).lstrip()
    parts = [body, _MEDIA_SPACER, rich.RICH_PHOTO_ANCHOR, _MEDIA_SPACER]
    if service_tail:
        parts.append(service_tail)
    return "\n\n".join(parts)


def _is_lost_carrier(error: str) -> bool:
    lowered = error.lower()
    return (
        "message to edit not found" in lowered
        or "message can't be edited" in lowered
        or "message_id_invalid" in lowered
    )
