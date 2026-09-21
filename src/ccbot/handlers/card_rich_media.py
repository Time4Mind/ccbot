"""Rich Markdown transport for live cards with an inline pane image."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardMarkup, Message
from telegram.error import BadRequest, RetryAfter, TimedOut

from .. import rich
from ..config import config
from ..session import session_manager
from ..session_models import Session
from .card_binding import bind_carrier, clear_carrier
from .card_types import CardState, CarrierKind
from .card_registry import lookup_session_for_message
from .kb_mode import _capture_pane_png

logger = logging.getLogger(__name__)

_SCREENSHOT_SAVE_INTERVAL = 60.0
_last_screenshot_save: dict[str, float] = {}

# Keep the same visible gap used between card events. Telegram collapses an
# empty ``<p><br></p>`` around media, while a non-breaking-space paragraph is
# preserved as its own rich block by every transport path.
_MEDIA_SPACER = "\u00a0"


@dataclass(frozen=True)
class RichCardSend:
    """A sent rich-media carrier and its reusable Telegram photo id."""

    message: Message
    photo_file_id: str


def remember_rich_photo(state: CardState, pane_hash: str, file_id: str) -> None:
    """Keep the three newest confirmed exact-image Telegram identifiers."""
    if not pane_hash or not file_id:
        return
    state.rich_media_cache = [
        item for item in state.rich_media_cache if item[0] != pane_hash
    ]
    state.rich_media_cache.append((pane_hash, file_id))
    del state.rich_media_cache[:-3]


def _discard_invalid_photo(state: CardState, sess: object | None, file_id: str) -> bool:
    """Forget one rejected Telegram photo id so a switch cannot revive it."""
    state.rich_media_cache = [
        item for item in state.rich_media_cache if item[1] != file_id
    ]
    if state.rich_media_file_id == file_id:
        state.rich_media_file_id = ""
    if not isinstance(sess, Session) or sess.screenshot_file_id != file_id:
        return False
    sess.screenshot_file_id = ""
    sess.screenshot_pane_hash = ""
    sess.screenshot_cached_at = 0.0
    _last_screenshot_save.pop(sess.id, None)
    return True


def persist_session_screenshot(
    sess: object | None, user_id: int, pane_hash: str, file_id: str
) -> None:
    """Persist the newest confirmed Telegram screenshot cache per session."""
    if not isinstance(sess, Session) or not pane_hash or not file_id:
        return
    now = time.time()
    settings = session_manager.get_user_settings(user_id)
    capture_kib = int(settings.get("screenshot_capture_kib", 48))
    profile = str(settings.get("screenshot_profile", "full8"))
    needs_save = (
        sess.screenshot_file_id != file_id
        or sess.screenshot_pane_hash != pane_hash
        or sess.screenshot_user_id != user_id
        or sess.screenshot_capture_kib != capture_kib
        or sess.screenshot_profile != profile
    )
    sess.screenshot_file_id = file_id
    sess.screenshot_pane_hash = pane_hash
    sess.screenshot_cached_at = now
    sess.screenshot_user_id = user_id
    sess.screenshot_capture_kib = capture_kib
    sess.screenshot_profile = profile
    # A busy rich card may receive a new Telegram file id every few seconds.
    # Keep every update in memory, but do not fsync the whole session state for
    # each frame.  Any unrelated state save includes the newest values; this
    # interval only bounds the dedicated crash-recovery checkpoint.
    last_save = _last_screenshot_save.get(sess.id, 0.0)
    if needs_save and now - last_save >= _SCREENSHOT_SAVE_INTERVAL:
        session_manager.save_state()
        _last_screenshot_save[sess.id] = now


async def send_rich_media_card(
    bot: Any,
    user_id: int,
    state: CardState,
    text: str,
    pane_png: bytes,
    *,
    reply_markup: InlineKeyboardMarkup | None,
    file_base_dir: Path | None = None,
) -> RichCardSend | None:
    """Send a rich card containing ``pane_png``; None requests text fallback."""
    if not config.rich_messages:
        return None
    try:
        message = await rich.send_rich_message(
            bot,
            user_id,
            _rich_card_markdown(text, state, file_base_dir=file_base_dir),
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
    """Edit text while keeping the terminal screenshot at its card anchor."""
    if state.msg_id is None or not config.rich_messages:
        return False

    sess_id = lookup_session_for_message(user_id, state.msg_id)
    sess = session_manager.get_session(sess_id) if sess_id else None
    workdir = getattr(sess, "workdir", "")
    file_base_dir = Path(workdir) if workdir else None
    window_id = sess.window_id if sess is not None else ""
    elapsed = time.monotonic() - state.last_photo_edit_ts

    photo: bytes | str | None = state.rich_media_file_id or None
    pane_hash = state.last_pane_hash
    uploaded_new_pane = False
    reused_cached_pane = False
    if window_id and (photo is None or refresh_pane and elapsed >= min_photo_interval):
        png, captured_hash = await _capture_pane_png(window_id, user_id=user_id)
        if (
            png is not None
            and captured_hash
            and (photo is None or captured_hash != state.last_pane_hash)
        ):
            pane_hash = captured_hash
            cached_file_id = next(
                (
                    file_id
                    for digest, file_id in reversed(state.rich_media_cache)
                    if digest == captured_hash
                ),
                "",
            )
            if cached_file_id:
                photo = cached_file_id
                reused_cached_pane = True
            else:
                photo = png
                uploaded_new_pane = True
        elif png is not None and captured_hash and photo is not None:
            # A real capture confirmed that the existing Telegram image is
            # still current. Advance both throttles without uploading it.
            pane_hash = captured_hash
            if captured_hash == state.last_pane_hash and text == state.last_rendered:
                state.last_photo_edit_ts = time.monotonic()
                return True
            reused_cached_pane = True

    if photo is None:
        logger.warning(
            "rich-media card has no reusable pane photo msg=%s", state.msg_id
        )
        return False

    try:
        result = await rich.edit_rich_message(
            bot,
            user_id,
            state.msg_id,
            _rich_card_markdown(
                text,
                state,
                file_base_dir=file_base_dir,
            ),
            reply_markup=reply_markup,
            photo=photo,
        )
    except RetryAfter:
        raise
    except TimedOut:
        # The server may have applied the idempotent edit before the client-side
        # timeout. Keep the rich carrier and let the next normal update converge;
        # downgrading it to text here causes the visible layout shift.
        logger.info(
            "rich-media card edit timed out msg=%s; keeping carrier", state.msg_id
        )
        return True
    except TimeoutError:
        # Our short rich-transport deadline is intentional: unlike PTB's
        # ambiguous full request timeout, it should immediately release the
        # normal text path so an agent response cannot remain visually stale.
        logger.info(
            "rich-media short deadline expired msg=%s; using text fallback",
            state.msg_id,
        )
        return False
    except BadRequest as exc:
        error = str(exc)
        if "message is not modified" in error.lower():
            return True
        if _is_lost_carrier(error):
            logger.info(
                "rich-media card lost carrier msg=%s err=%s", state.msg_id, error
            )
            clear_carrier(state)
            return False
        if "rich_message_photo_no_media_found" in error.lower():
            # Telegram says this concrete message no longer owns the embedded
            # photo that rich editing expects. Retrying the same carrier can
            # never heal it and previously triggered rich -> rich-text ->
            # Markdown fallback storms every few seconds. Release it once;
            # the next normal card update will create a fresh rich carrier.
            logger.warning(
                "rich-media card missing photo; releasing carrier msg=%s",
                state.msg_id,
            )
            clear_carrier(state)
            return False
        if "rich_message_photo_invalid" in error.lower() and isinstance(photo, str):
            invalidated_session = _discard_invalid_photo(state, sess, photo)
            png, captured_hash = await _capture_pane_png(window_id, user_id=user_id)
            if png is not None and captured_hash:
                try:
                    result = await rich.edit_rich_message(
                        bot,
                        user_id,
                        state.msg_id,
                        _rich_card_markdown(
                            text,
                            state,
                            file_base_dir=file_base_dir,
                        ),
                        reply_markup=reply_markup,
                        photo=png,
                    )
                except RetryAfter:
                    raise
                except TimedOut:
                    if invalidated_session:
                        session_manager.save_state()
                    logger.info(
                        "rich-media fresh-photo retry timed out msg=%s; keeping carrier",
                        state.msg_id,
                    )
                    return True
                except Exception as retry_exc:
                    if invalidated_session:
                        session_manager.save_state()
                    logger.warning(
                        "rich-media fresh-photo retry failed msg=%s: %s",
                        state.msg_id,
                        retry_exc,
                    )
                    return False
                pane_hash = captured_hash
                uploaded_new_pane = True
                reused_cached_pane = False
            else:
                if invalidated_session:
                    session_manager.save_state()
                logger.info(
                    "rich-media invalid photo removed msg=%s; fresh capture unavailable",
                    state.msg_id,
                )
                return False
        else:
            logger.warning(
                "rich-media card edit failed msg=%s: %s", state.msg_id, error
            )
            return False
    except Exception as exc:
        # Transport/read failures do not prove that the carrier is invalid.
        # Keep the last screenshot in place; the status-driven refresh retries
        # within four seconds while the pane is working. Falling through to a
        # text edit here caused the user-visible rich-card layout shift.
        logger.warning(
            "rich-media card edit transient failure msg=%s; keeping carrier: %s",
            state.msg_id,
            exc,
        )
        return True

    if uploaded_new_pane:
        new_file_id = rich.extract_rich_photo_file_id(result)
        # If a local Bot API proxy returned only True, do not reuse the old
        # id on the next text edit: that would restore the previous image.
        bind_carrier(
            state,
            state.msg_id,
            CarrierKind.RICH_MEDIA,
            rich_media_file_id=new_file_id or "",
            pane_hash=pane_hash,
            photo_edit_ts=time.monotonic(),
        )
        remember_rich_photo(state, pane_hash, new_file_id or "")
        persist_session_screenshot(sess, user_id, pane_hash, new_file_id or "")
    elif reused_cached_pane:
        bind_carrier(
            state,
            state.msg_id,
            CarrierKind.RICH_MEDIA,
            rich_media_file_id=str(photo),
            pane_hash=pane_hash,
            photo_edit_ts=time.monotonic(),
        )
        persist_session_screenshot(sess, user_id, pane_hash, str(photo))
    return True


def _rich_card_markdown(
    text: str, state: CardState, *, file_base_dir: Path | None = None
) -> str:
    """Insert the spaced photo before context/background service metadata."""
    offset = state.media_anchor_offset
    if offset <= 0 or offset > len(text):
        return rich.to_rich_markdown(text, file_base_dir=file_base_dir)
    body = rich.to_rich_markdown(text[:offset], file_base_dir=file_base_dir).rstrip()
    service_tail = rich.to_rich_markdown(
        text[offset:].lstrip(), file_base_dir=file_base_dir
    ).lstrip()
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
