"""Tests for the Bot API 10.1 rich-message layer (rich.py + safe_* wiring).

Covers to_rich_markdown escaping rules (bare ``<`` vs supported tags vs
code spans), expandable-quote → <details> conversion, and the
rich-first / MarkdownV2-fallback behaviour of safe_send and safe_edit.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ccbot import rich
from ccbot.config import config
from ccbot.handlers import message_sender
from ccbot.transcript_format import format_expandable_quote


class TestToRichMarkdown:
    def test_bare_lt_escaped(self) -> None:
        assert rich.to_rich_markdown("a < b") == "a &lt; b"

    def test_tag_shaped_fragment_escaped(self) -> None:
        # x<y>z would be silently rendered as "xz" by the rich parser
        assert rich.to_rich_markdown("x<y>z list<int>") == ("x&lt;y>z list&lt;int>")

    def test_supported_tag_preserved(self) -> None:
        text = "<b>bold</b> and <tg-spoiler>hidden</tg-spoiler>"
        assert rich.to_rich_markdown(text) == text

    def test_supported_tag_with_attrs_preserved(self) -> None:
        text = '<a href="https://t.me/">link</a>'
        assert rich.to_rich_markdown(text) == text

    def test_lt_in_fenced_code_preserved(self) -> None:
        text = "```html\n<div>hi</div>\n```"
        assert rich.to_rich_markdown(text) == text

    def test_lt_in_inline_code_preserved(self) -> None:
        text = "use `a<b>` here"
        assert rich.to_rich_markdown(text) == text

    def test_lt_after_code_span_escaped(self) -> None:
        assert rich.to_rich_markdown("`ok<x>` then a<b c") == "`ok<x>` then a&lt;b c"

    def test_unterminated_fence_preserved(self) -> None:
        text = "```\n<streaming>"
        assert rich.to_rich_markdown(text) == text

    def test_expandable_quote_becomes_details(self) -> None:
        out = rich.to_rich_markdown(format_expandable_quote("first line\nrest"))
        assert "<details><summary>first line</summary>" in out
        assert "first line\nrest" in out
        assert out.endswith("</details>\n")
        assert "\x02" not in out

    def test_expandable_quote_long_summary_truncated(self) -> None:
        out = rich.to_rich_markdown(format_expandable_quote("x" * 200))
        summary = out.split("<summary>")[1].split("</summary>")[0]
        assert len(summary) <= 64
        assert summary.endswith("…")

    def test_expandable_quote_inner_lt_escaped(self) -> None:
        out = rich.to_rich_markdown(format_expandable_quote("a<y>c"))
        assert "a&lt;y>c" in out


def _sent_message_json() -> dict[str, Any]:
    return {
        "message_id": 42,
        "date": 0,
        "chat": {"id": 449, "type": "private"},
    }


class _FakeBot:
    """Minimal stand-in for ExtBot: records _post calls."""

    def __init__(self, post_result: Any = None, post_error: Exception | None = None):
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._post_result = post_result
        self._post_error = post_error
        self.send_message = AsyncMock(return_value="md-fallback-message")

    async def _post(self, endpoint: str, data: dict[str, Any]) -> Any:
        self.posts.append((endpoint, data))
        if self._post_error is not None:
            raise self._post_error
        return self._post_result


@pytest.fixture
def rich_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "rich_messages", True)


@pytest.fixture
def rich_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "rich_messages", False)


class TestSafeSendRichPath:
    @pytest.mark.asyncio
    async def test_rich_send_used_when_enabled(self, rich_on: None) -> None:
        bot = _FakeBot(post_result=_sent_message_json())
        msg = await message_sender.safe_send(bot, 449, "a < b")  # type: ignore[arg-type]
        assert msg is not None and msg.message_id == 42
        assert bot.posts == [
            (
                "sendRichMessage",
                {"chat_id": 449, "rich_message": {"markdown": "a &lt; b"}},
            )
        ]
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_markup_forwarded(self, rich_on: None) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        bot = _FakeBot(post_result=_sent_message_json())
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("x", callback_data="y")]])
        await message_sender.safe_send(bot, 449, "hi", reply_markup=markup)  # type: ignore[arg-type]
        assert bot.posts[0][1]["reply_markup"] is markup

    @pytest.mark.asyncio
    async def test_fallback_to_markdownv2_on_rich_error(self, rich_on: None) -> None:
        bot = _FakeBot(post_error=RuntimeError("boom"))
        msg = await message_sender.safe_send(bot, 449, "hello")  # type: ignore[arg-type]
        assert msg == "md-fallback-message"
        bot.send_message.assert_called_once()
        assert bot.send_message.call_args.kwargs["parse_mode"] == "MarkdownV2"

    @pytest.mark.asyncio
    async def test_rich_disabled_goes_straight_to_markdownv2(
        self, rich_off: None
    ) -> None:
        bot = _FakeBot(post_result=_sent_message_json())
        await message_sender.safe_send(bot, 449, "hello")  # type: ignore[arg-type]
        assert bot.posts == []
        bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_after_propagates(self, rich_on: None) -> None:
        from telegram.error import RetryAfter

        bot = _FakeBot(post_error=RetryAfter(3))
        with pytest.raises(RetryAfter):
            await message_sender.safe_send(bot, 449, "hello")  # type: ignore[arg-type]


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeMessage:
    def __init__(self, bot: _FakeBot, chat_id: int = 449, message_id: int = 7) -> None:
        self._bot = bot
        self.chat = _FakeChat(chat_id)
        self.message_id = message_id
        self.edit_message_text = AsyncMock()


class TestSafeEditRichPath:
    @pytest.mark.asyncio
    async def test_rich_edit_used_when_enabled(self, rich_on: None) -> None:
        bot = _FakeBot(post_result=True)
        bot.edit_message_text = AsyncMock()  # type: ignore[attr-defined]
        target = _FakeMessage(bot)
        await message_sender.safe_edit(target, "new < text")
        assert bot.posts == [
            (
                "editMessageText",
                {
                    "chat_id": 449,
                    "message_id": 7,
                    "rich_message": {"markdown": "new &lt; text"},
                },
            )
        ]
        bot.edit_message_text.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_not_modified_treated_as_success(self, rich_on: None) -> None:
        from telegram.error import BadRequest

        bot = _FakeBot(post_error=BadRequest("Message is not modified: blah"))
        bot.edit_message_text = AsyncMock()  # type: ignore[attr-defined]
        target = _FakeMessage(bot)
        await message_sender.safe_edit(target, "same text")
        # must NOT downgrade the rich message via the MarkdownV2 fallback
        bot.edit_message_text.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_fallback_to_markdownv2_on_rich_error(self, rich_on: None) -> None:
        bot = _FakeBot(post_error=RuntimeError("boom"))
        bot.edit_message_text = AsyncMock()  # type: ignore[attr-defined]
        target = _FakeMessage(bot)
        await message_sender.safe_edit(target, "hello")
        bot.edit_message_text.assert_called_once()  # type: ignore[attr-defined]
