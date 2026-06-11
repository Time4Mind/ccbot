"""Safe message sending helpers with rich-message → MarkdownV2 fallback.

Provides utility functions for sending Telegram messages with automatic
format conversion and graceful degradation: Bot API 10.1 rich messages
(native markdown, when `config.rich_messages` is on) → MarkdownV2 via
telegramify-markdown → plain text.

Functions:
  - send_with_fallback: Send with formatting → plain text fallback
  - send_photo: Photo sending (single or media group)
  - safe_reply: Reply with formatting, fallback to plain text
  - safe_edit: Edit message with formatting, fallback to plain text
  - safe_send: Send message with formatting, fallback to plain text

Rate limiting is handled globally by AIORateLimiter on the Application.
RetryAfter exceptions are re-raised so callers (queue worker) can handle them.
"""

import io
import logging
from typing import Any

from telegram import Bot, InputMediaPhoto, LinkPreviewOptions, Message
from telegram.error import BadRequest, RetryAfter

from .. import rich
from ..config import config
from ..markdown_v2 import convert_markdown
from ..transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)


def strip_sentinels(text: str) -> str:
    """Strip expandable quote sentinel markers for plain text fallback."""
    for s in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(s, "")
    return text


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


PARSE_MODE = "MarkdownV2"


# Disable link previews in all messages to reduce visual noise
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def _try_rich_send(
    bot: Bot, chat_id: int, text: str, kwargs: dict[str, Any]
) -> Message | None:
    """Attempt a Bot API 10.1 rich send; None means fall back to MarkdownV2.

    Only ``reply_markup`` survives from kwargs — sendRichMessage has no
    parse_mode / link_preview_options equivalents.
    """
    if not config.rich_messages:
        return None
    try:
        return await rich.send_rich_message(
            bot,  # type: ignore[arg-type]
            chat_id,
            rich.to_rich_markdown(text),
            reply_markup=kwargs.get("reply_markup"),
        )
    except RetryAfter:
        raise
    except Exception as e:
        logger.warning("rich send failed chat=%s, falling back: %s", chat_id, e)
        return None


async def _try_rich_edit(target: Any, text: str, kwargs: dict[str, Any]) -> bool:
    """Attempt a rich edit on a Message/CallbackQuery target.

    Returns True when the edit landed (or the content was already
    identical — "message is not modified" must NOT trigger the MarkdownV2
    fallback, or an unchanged rich message would get visibly downgraded).
    """
    if not config.rich_messages:
        return False
    msg_obj = getattr(target, "message", target)
    chat_id = getattr(getattr(msg_obj, "chat", None), "id", None)
    msg_id = getattr(msg_obj, "message_id", None)
    bot = getattr(target, "_bot", None) or getattr(msg_obj, "_bot", None)
    if bot is None or chat_id is None or msg_id is None:
        return False
    try:
        await rich.edit_rich_message(
            bot,
            chat_id,
            msg_id,
            rich.to_rich_markdown(text),
            reply_markup=kwargs.get("reply_markup"),
        )
        return True
    except RetryAfter:
        raise
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return True
        logger.warning("rich edit failed msg=%s, falling back: %s", msg_id, e)
        return False
    except Exception as e:
        logger.warning("rich edit failed msg=%s, falling back: %s", msg_id, e)
        return False


async def send_with_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Send message with MarkdownV2, falling back to plain text on failure.

    Returns the sent Message on success, None on failure.
    RetryAfter is re-raised for caller handling.
    """
    rich_msg = await _try_rich_send(bot, chat_id, text, kwargs)
    if rich_msg is not None:
        return rich_msg
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    **kwargs: Any,
) -> None:
    """Send photo(s) to chat. Sends as media group if multiple images.

    Rate limiting is handled globally by AIORateLimiter on the Application.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        image_data: List of (media_type, raw_bytes) tuples
        **kwargs: Extra kwargs passed to send_photo/send_media_group
    """
    if not image_data:
        return
    try:
        if len(image_data) == 1:
            _media_type, raw_bytes = image_data[0]
            await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(raw_bytes),
                **kwargs,
            )
        else:
            media = [
                InputMediaPhoto(media=io.BytesIO(raw_bytes))
                for _media_type, raw_bytes in image_data
            ]
            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send photo to %d: %s", chat_id, e)


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with formatting, falling back to plain text on failure."""
    reply_bot = getattr(message, "_bot", None)
    reply_chat_id = getattr(message, "chat_id", None) or getattr(
        getattr(message, "chat", None), "id", None
    )
    if reply_bot is not None and reply_chat_id is not None:
        rich_msg = await _try_rich_send(reply_bot, reply_chat_id, text, kwargs)
        if rich_msg is not None:
            return rich_msg
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await message.reply_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await message.reply_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to reply: {e}")
            raise


async def _do_edit(
    target: Any, text: str, *, parse_mode: str | None, **kwargs: Any
) -> Any:
    """Direct ``bot.edit_message_text`` if we have a Message/CallbackQuery,
    falling back to the shortcut method otherwise.

    The shortcut ``CallbackQuery.edit_message_text`` was producing silent
    no-ops in production (logs reported success, ``forwardMessage`` showed
    no change) while a fresh ``Bot.edit_message_text`` from a script
    against the same ``message_id`` did update the message. Pinning the
    explicit chat/message ids removes whatever per-callback state was
    interfering.
    """
    msg_obj = getattr(target, "message", target)
    chat_id = getattr(getattr(msg_obj, "chat", None), "id", None)
    msg_id = getattr(msg_obj, "message_id", None)
    bot = getattr(target, "_bot", None) or getattr(msg_obj, "_bot", None)
    if bot is None or chat_id is None or msg_id is None:
        # Last-resort shortcut path for unusual targets.
        return await target.edit_message_text(text, parse_mode=parse_mode, **kwargs)
    return await bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg_id,
        text=text,
        parse_mode=parse_mode,
        **kwargs,
    )


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    """Edit message with formatting, falling back to plain text on failure."""
    msg_obj = getattr(target, "message", None)
    msg_id = getattr(msg_obj, "message_id", None)

    if await _try_rich_edit(target, text, kwargs):
        logger.info(
            "safe_edit ok msg=%s len=%d mode=rich",
            msg_id,
            len(text),
            extra={
                "event": "safe_edit_ok",
                "msg_id": msg_id,
                "len": len(text),
                "mode": "rich",
            },
        )
        return

    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        await _do_edit(target, _ensure_formatted(text), parse_mode=PARSE_MODE, **kwargs)
        logger.info(
            "safe_edit ok msg=%s len=%d mode=md",
            msg_id,
            len(text),
            extra={
                "event": "safe_edit_ok",
                "msg_id": msg_id,
                "len": len(text),
                "mode": "md",
            },
        )
    except RetryAfter:
        raise
    except Exception as md_err:
        # Log the MarkdownV2 failure too — used to be silent, but the
        # plain-text fallback can land short/garbled when the converter
        # mangled the text, and we need to see the original parse error
        # to investigate ("история исчезла" reports).
        logger.warning("safe_edit MarkdownV2 failed msg=%s err=%s", msg_id, md_err)
        try:
            await _do_edit(target, strip_sentinels(text), parse_mode=None, **kwargs)
            logger.info(
                "safe_edit ok msg=%s len=%d mode=plain",
                msg_id,
                len(text),
                extra={
                    "event": "safe_edit_ok",
                    "msg_id": msg_id,
                    "len": len(text),
                    "mode": "plain",
                },
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("safe_edit fallback also failed msg=%s err=%s", msg_id, e)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Send message with formatting, falling back to plain text on failure.

    Returns the sent Message on success, None on failure.
    """
    rich_msg = await _try_rich_send(bot, chat_id, text, kwargs)
    if rich_msg is not None:
        return rich_msg
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None
