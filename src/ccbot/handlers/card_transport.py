"""Send and edit text or photo-backed live cards through Telegram."""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter

from ..session import Session, session_manager
from .card_model import (
    CardState,
    _card_is_busy,
)
from .card_rich_media import edit_rich_media_card, send_rich_media_card
from .kb_mode import _capture_pane_png
from .card_registry import (
    _carrier_edit_lock,
    _user_send_lock,
    _strip_stale_switchers,
    _register_msg,
    lookup_session_for_message,
    _inline_screens_enabled,
    _legacy,
)

logger = logging.getLogger(__name__)


__all__ = [
    "_send_card",
    "_send_card_locked",
    "_edit_card",
    "_edit_card_unlocked",
    "_PHOTO_EDIT_MIN_INTERVAL",
    "_edit_photo_card",
    "_deferred_edit",
]


async def _send_card(
    bot: Bot,
    user_id: int,
    sess: Session,
    state: CardState,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send a brand-new card message and remember it as the live card.

    Serialized per user (``_user_send_lock``) so concurrent spawns from
    two different sessions can't interleave the send / strip / pointer
    update and desync which message carries the live switcher.
    """
    async with _user_send_lock(user_id):
        await _send_card_locked(
            bot, user_id, sess, state, text=text, reply_markup=reply_markup
        )


async def _send_card_locked(
    bot: Bot,
    user_id: int,
    sess: Session,
    state: CardState,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Body of :func:`_send_card`; call only with the user send-lock held.

    ``reply_markup`` overrides the default footer keyboard. Used by
    ``finalize_task`` to attach the idle-state Kill row to a completed
    result instead of the busy-state Stop row.
    """
    if reply_markup is None:
        # Default: a fresh card is being sent because a turn is in
        # flight (update_session_card, repost_card, continuation
        # overflow). ``_card_is_busy`` keys off ``state.msg_id`` which
        # is still None at this point — the right signal here is
        # "we're sending a card", which by definition means Stop is
        # the user's intent. ``finalize_task`` overrides ``reply_markup``
        # explicitly with the Kill keyboard when a turn completes.
        reply_markup = _legacy("build_footer_keyboard")(
            user_id, screen="main", is_busy=True
        )
    keyboard = reply_markup

    # Inline screenshots ON: prefer a Rich Markdown card whose final block
    # is the pane image. Older/rich-disabled servers retain photo+caption.
    sent = None
    if _inline_screens_enabled(user_id) and sess.window_id:
        from ..markdown_v2 import convert_markdown
        from .message_sender import PARSE_MODE, strip_sentinels

        png, pane_hash = await _capture_pane_png(sess.window_id)
        if png is not None:
            rich_sent = await send_rich_media_card(
                bot,
                user_id,
                text,
                png,
                reply_markup=keyboard,
            )
            if rich_sent is not None:
                sent = rich_sent.message
                state.is_rich_media_msg = True
                state.rich_media_file_id = rich_sent.photo_file_id
                state.is_photo_msg = False
                state.last_pane_hash = pane_hash
                state.last_photo_edit_ts = time.monotonic()

        if png is not None and sent is None:
            import io as _io

            caption = convert_markdown(text)
            try:
                sent = await bot.send_photo(
                    chat_id=user_id,
                    photo=_io.BytesIO(png),
                    caption=caption,
                    parse_mode=PARSE_MODE,
                    reply_markup=keyboard,
                    disable_notification=True,
                )
            except RetryAfter:
                raise
            except Exception as e:
                logger.debug("photo send failed, retry plain caption: %s", e)
                try:
                    sent = await bot.send_photo(
                        chat_id=user_id,
                        photo=_io.BytesIO(png),
                        caption=strip_sentinels(text),
                        reply_markup=keyboard,
                        disable_notification=True,
                    )
                except Exception as e2:
                    logger.debug("photo send plain fallback failed: %s", e2)
            if sent is not None:
                state.is_rich_media_msg = False
                state.rich_media_file_id = ""
                state.is_photo_msg = True
                state.last_pane_hash = pane_hash
                state.last_photo_edit_ts = time.monotonic()

    # Text-mode card OR photo path failed → text fallback.
    if sent is None:
        from .message_sender import send_with_fallback

        try:
            sent = await send_with_fallback(
                bot,
                user_id,
                text,
                reply_markup=keyboard,
                disable_notification=True,
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.debug("card send failed: %s", e)
            return
        if sent is None:
            return
        state.is_rich_media_msg = False
        state.rich_media_file_id = ""
        state.is_photo_msg = False
    # Exactly one message in the chat may carry a live switcher, and it is
    # this one — strip every other card's keyboard, not just the pointer's.
    state.msg_id = sent.message_id
    await _strip_stale_switchers(bot, user_id, sent.message_id, sess.id)
    if keyboard is not None:
        session_manager.set_last_switcher_msg(user_id, sent.message_id)
    state.last_rendered = text
    _register_msg(user_id, sent.message_id, sess.id)
    session_manager.set_card_msg(user_id, sent.message_id)


async def _edit_card(
    bot: Bot,
    user_id: int,
    state: CardState,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    refresh_pane: bool = True,
) -> bool:
    """Serialize Telegram edits with cross-session carrier hand-offs."""
    async with _carrier_edit_lock(user_id):
        return await _edit_card_unlocked(
            bot,
            user_id,
            state,
            text=text,
            reply_markup=reply_markup,
            refresh_pane=refresh_pane,
        )


async def _edit_card_unlocked(
    bot: Bot,
    user_id: int,
    state: CardState,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    refresh_pane: bool = True,
) -> bool:
    """Edit the live card. Returns False if the edit failed permanently.

    Always sends a keyboard along with the text — relying on Telegram's
    "preserve keyboard when reply_markup is omitted" semantics turned out
    flaky (the buttons flickered between edits). Caller may pass an
    explicit `reply_markup`; otherwise we rebuild from current busy state.
    """
    if state.msg_id is None:
        return False
    # User is currently looking at a Menu / sub-screen on this card's
    # message. Don't repaint — would clobber whatever they're navigating.
    # State.lines keeps accumulating; resume_card_view will catch up.
    if state.in_menu_view:
        return True
    if reply_markup is None:
        reply_markup = _legacy("build_footer_keyboard")(
            user_id, screen="main", is_busy=_card_is_busy(state)
        )
    from ..markdown_v2 import convert_markdown
    from .message_sender import (
        NO_LINK_PREVIEW,
        PARSE_MODE,
        strip_sentinels,
        try_rich_edit,
    )

    # Rich-media card: keep the pane as the final block and reuse its
    # file_id for text-only edits. A changed pane is uploaded at most once
    # per throttle window.
    if state.is_rich_media_msg:
        return await edit_rich_media_card(
            bot,
            user_id,
            state,
            text=text,
            reply_markup=reply_markup,
            min_photo_interval=_PHOTO_EDIT_MIN_INTERVAL,
            refresh_pane=refresh_pane,
        )

    # Legacy photo-mode card: editMessageMedia when pane changed (≤1 per 3s),
    # else editMessageCaption to refresh just the text. Captions have no
    # rich-message equivalent, so this path stays MarkdownV2.
    if state.is_photo_msg:
        return await _edit_photo_card(
            bot,
            user_id,
            state,
            text=text,
            formatted=convert_markdown(text),
            reply_markup=reply_markup,
            refresh_pane=refresh_pane,
        )

    # Rich-first (Bot API 10.1): keeps the card's native rendering (GFM
    # tables, headings, <details>) consistent with the rich _send_card
    # path — otherwise the first edit would visibly downgrade the card
    # to MarkdownV2. On failure (rich off, API error, lost carrier) fall
    # through to the MarkdownV2 pipeline below, which also owns the
    # lost-carrier detection.
    if await try_rich_edit(bot, user_id, state.msg_id, text, reply_markup=reply_markup):
        return True

    formatted = convert_markdown(text)

    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state.msg_id,
            text=formatted,
            parse_mode=PARSE_MODE,
            reply_markup=reply_markup,
            link_preview_options=NO_LINK_PREVIEW,
        )
        return True
    except BadRequest as e:
        err = str(e)
        if "Message is not modified" in err:
            return True
        if (
            "Message to edit not found" in err
            or "message can't be edited" in err.lower()
            or "MESSAGE_ID_INVALID" in err
        ):
            # Carrier is genuinely gone — reset msg_id so the next event
            # opens a fresh card.
            logger.info("card edit lost-carrier msg_id=%s err=%s", state.msg_id, err)
            state.msg_id = None
            return False
        # Parse error / can't render — fall back to stripped plain text
        # on the SAME carrier. Keep the card alive.
        logger.warning("card edit MarkdownV2 failed msg=%s err=%s", state.msg_id, err)
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=state.msg_id,
                text=strip_sentinels(text),
                reply_markup=reply_markup,
                link_preview_options=NO_LINK_PREVIEW,
            )
            return True
        except BadRequest as e2:
            err2 = str(e2)
            if "Message is not modified" in err2:
                return True
            logger.warning(
                "card edit plain fallback failed msg=%s err=%s", state.msg_id, err2
            )
        except RetryAfter:
            raise
        except Exception as e2:
            logger.warning(
                "card edit plain fallback exc msg=%s err=%s", state.msg_id, e2
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.warning("card edit failed (other): %s", e)
    return False


_PHOTO_EDIT_MIN_INTERVAL = 2.5  # seconds — per-session throttle on editMessageMedia


async def _edit_photo_card(
    bot: Bot,
    user_id: int,
    state: CardState,
    *,
    text: str,
    formatted: str,
    reply_markup: InlineKeyboardMarkup | None,
    refresh_pane: bool = True,
) -> bool:
    """Edit a photo+caption card msg.

    Refresh strategy:
    * Pane unchanged since last edit → editMessageCaption only.
    * Pane changed AND ≥3s since last photo edit → editMessageMedia
      with new photo + new caption + keyboard.
    * Pane changed but throttled → editMessageCaption only. Next render
      after the throttle window will pick up the freshest pane.
    """
    import io as _io

    from telegram import InputMediaPhoto

    from ..markdown_v2 import convert_markdown
    from .message_sender import PARSE_MODE, strip_sentinels

    # Resolve session from msg_id lookup (we don't have it here directly).
    # Find by reverse mapping (user_id, msg_id) → session_id.
    sess_id = lookup_session_for_message(user_id, state.msg_id or 0)
    sess = session_manager.get_session(sess_id) if sess_id else None
    window_id = sess.window_id if sess is not None else ""

    pane_changed = False
    pane_png: bytes | None = None
    pane_hash = state.last_pane_hash
    elapsed = time.monotonic() - state.last_photo_edit_ts
    if refresh_pane and window_id and elapsed >= _PHOTO_EDIT_MIN_INTERVAL:
        png, h = await _capture_pane_png(window_id)
        if png is not None and h:
            if h != state.last_pane_hash:
                pane_changed = True
                pane_png = png
                pane_hash = h

    try:
        if pane_changed and pane_png is not None:
            media = InputMediaPhoto(
                media=_io.BytesIO(pane_png),
                caption=convert_markdown(text),
                parse_mode=PARSE_MODE,
            )
            await bot.edit_message_media(
                chat_id=user_id,
                message_id=state.msg_id,
                media=media,
                reply_markup=reply_markup,
            )
            state.last_pane_hash = pane_hash
            state.last_photo_edit_ts = time.monotonic()
            return True
        # Pane unchanged or throttled — caption-only refresh.
        await bot.edit_message_caption(
            chat_id=user_id,
            message_id=state.msg_id,
            caption=formatted,
            parse_mode=PARSE_MODE,
            reply_markup=reply_markup,
        )
        return True
    except BadRequest as e:
        err = str(e)
        if "Message is not modified" in err:
            return True
        if (
            "Message to edit not found" in err
            or "message can't be edited" in err.lower()
            or "MESSAGE_ID_INVALID" in err
        ):
            logger.info("photo card edit lost-carrier msg=%s err=%s", state.msg_id, err)
            state.msg_id = None
            return False
        logger.warning(
            "photo card edit MarkdownV2 failed msg=%s err=%s", state.msg_id, err
        )
        # Plain-text caption fallback.
        try:
            await bot.edit_message_caption(
                chat_id=user_id,
                message_id=state.msg_id,
                caption=strip_sentinels(text),
                reply_markup=reply_markup,
            )
            return True
        except Exception as e2:
            logger.warning(
                "photo card plain fallback failed msg=%s err=%s", state.msg_id, e2
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.warning("photo card edit failed (other): %s", e)
    return False


async def _deferred_edit(
    bot: Bot, user_id: int, sess: Session, state: CardState, delay: float
) -> None:
    """Sleep `delay` then render the latest card state and edit once.

    The deferred task always picks up the latest `state.events`, so multiple
    events arriving during the sleep collapse into a single edit.
    """
    try:
        await asyncio.sleep(delay)
        # Stale guard: card may have been reset (finalize_task) while we slept.
        if state.msg_id is None:
            return
        text = _legacy("_render_card")(sess, state, user_id=user_id)
        if text == state.last_rendered:
            return
        state.pending_edit_in_flight = True
        if await _legacy("_edit_card")(bot, user_id, state, text=text):
            state.last_rendered = text
            state.last_edit_ts = time.monotonic()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.debug("deferred card edit failed: %s", e)
    finally:
        state.pending_edit_in_flight = False
        state.pending_edit = None
