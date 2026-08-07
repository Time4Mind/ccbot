"""Tests for the Bot API 10.2 rich-message layer (rich.py + safe_* wiring).

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

    def test_single_line_fence_becomes_copyable_inline_code(self) -> None:
        text = "Команда:\n\n```bash\nuv sync && uv run ccbot\n```"
        assert rich.to_rich_markdown(text) == ("Команда:\n\n`uv sync && uv run ccbot`")

    def test_single_line_fence_preserves_lt_inside_inline_code(self) -> None:
        text = "```bash\nprintf '%s\\n' 'a<b'\n```"
        assert rich.to_rich_markdown(text) == "`printf '%s\\n' 'a<b'`"

    def test_multiline_shell_fence_becomes_rich_code(self) -> None:
        text = "```bash\nuv sync\nuv run ccbot\n```"
        assert rich.to_rich_markdown(text) == "<code>uv sync\nuv run ccbot</code>"

    def test_multiline_shell_fence_becomes_copyable_rich_code(self) -> None:
        assert (
            rich.to_rich_markdown("```bash\nuv sync\nuv run ccbot\n```")
            == "<code>uv sync\nuv run ccbot</code>"
        )
        assert (
            rich.to_rich_markdown("```\ngit fetch origin\ngit status\n```")
            == "<code>git fetch origin\ngit status</code>"
        )

    def test_single_line_and_non_shell_fences_stay_rich(self) -> None:
        assert rich.to_rich_markdown("```bash\nuv run ccbot\n```") == "`uv run ccbot`"
        assert (
            rich.to_rich_markdown("```python\nimport os\nprint(os.getcwd())\n```")
            == "```python\nimport os\nprint(os.getcwd())\n```"
        )

    def test_multiline_shell_code_escapes_html_metacharacters(self) -> None:
        assert rich.to_rich_markdown(
            "```sh\nprintf '<ok>' && echo done > out\nprintf 'a&b'\n```"
        ) == (
            "<code>printf '&lt;ok&gt;' &amp;&amp; echo done &gt; out\n"
            "printf 'a&amp;b'</code>"
        )

    def test_single_line_fence_with_backtick_stays_fenced(self) -> None:
        text = "```bash\necho `date`\n```"
        assert rich.to_rich_markdown(text) == text

    def test_single_line_non_shell_fence_keeps_language_formatting(self) -> None:
        text = "```python\nprint('hello')\n```"
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


class TestSubWrapTables:
    def test_cells_wrapped_in_sub(self) -> None:
        table = "| a | b |\n|---|---|\n| 1 | 2 |"
        out = rich.to_rich_markdown(table)
        assert "| <sub>a</sub> | <sub>b</sub> |" in out
        assert "| <sub>1</sub> | <sub>2</sub> |" in out

    def test_separator_row_untouched(self) -> None:
        table = "| a | b |\n|:---|---:|\n| 1 | 2 |"
        out = rich.to_rich_markdown(table)
        assert "|:---|---:|" in out

    def test_empty_cells_untouched(self) -> None:
        table = "| a |  |\n|---|---|\n| 1 | 2 |"
        out = rich.to_rich_markdown(table)
        assert "| <sub>a</sub> |  |" in out

    def test_already_sub_not_double_wrapped(self) -> None:
        table = "| <sub>a</sub> | b |\n|---|---|\n| 1 | 2 |"
        out = rich.to_rich_markdown(table)
        assert "<sub><sub>" not in out

    def test_inline_formatting_kept_inside_sub(self) -> None:
        table = "| **bold** | `code` |\n|---|---|\n| x | y |"
        out = rich.to_rich_markdown(table)
        assert "| <sub>**bold**</sub> | <sub>`code`</sub> |" in out

    def test_single_pipe_line_not_a_table(self) -> None:
        text = "| just one line with pipes |\nplain text"
        out = rich.to_rich_markdown(text)
        assert "<sub>" not in out

    def test_pipe_lines_inside_code_fence_untouched(self) -> None:
        text = "```\n| a | b |\n| 1 | 2 |\n```"
        out = rich.to_rich_markdown(text)
        assert "<sub>" not in out

    def test_table_after_code_fence_wrapped(self) -> None:
        text = "```\ncode\n```\n| a | b |\n|---|---|\n| 1 | 2 |"
        out = rich.to_rich_markdown(text)
        assert "| <sub>a</sub> | <sub>b</sub> |" in out


class TestBlankLineBeforeTables:
    def test_blank_inserted_after_caption(self) -> None:
        out = rich.to_rich_markdown("**В работе**\n| a | b |\n|---|---|\n| 1 | 2 |")
        assert "**В работе**\n\n| <sub>a</sub>" in out

    def test_existing_blank_not_doubled(self) -> None:
        out = rich.to_rich_markdown("caption\n\n| a | b |\n|---|---|\n| 1 | 2 |")
        assert "caption\n\n| <sub>a</sub>" in out
        assert "caption\n\n\n" not in out

    def test_table_at_start_untouched(self) -> None:
        out = rich.to_rich_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
        assert out.startswith("| <sub>a</sub>")

    def test_no_blank_when_next_line_not_separator(self) -> None:
        # a lone pipe line after text is not a table — no blank injected
        text = "caption\n| just pipes |\nmore"
        out = rich.to_rich_markdown(text)
        assert "caption\n| just pipes |" in out

    def test_pipe_table_inside_fence_untouched(self) -> None:
        text = "caption\n```\n| a | b |\n|---|---|\n```"
        out = rich.to_rich_markdown(text)
        assert "caption\n```\n| a | b |" in out


def _sent_message_json() -> dict[str, Any]:
    return {
        "message_id": 42,
        "date": 0,
        "chat": {"id": 449, "type": "private"},
    }


def _sent_rich_photo_json() -> dict[str, Any]:
    return {
        **_sent_message_json(),
        "rich_message": {
            "blocks": [
                {"type": "paragraph", "text": {"text": "status"}},
                {
                    "type": "photo",
                    "photo": [
                        {
                            "file_id": "photo-small",
                            "file_unique_id": "small-unique",
                            "width": 90,
                            "height": 60,
                            "file_size": 100,
                        },
                        {
                            "file_id": "photo-large",
                            "file_unique_id": "large-unique",
                            "width": 1280,
                            "height": 720,
                            "file_size": 20_000,
                        },
                    ],
                },
            ]
        },
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


class TestRichPhotoMedia:
    @pytest.mark.asyncio
    async def test_send_uploads_photo_as_separate_multipart_field(self) -> None:
        from telegram import InputFile

        photo_bytes = b"\x89PNG\r\n\x1a\nterminal screenshot"
        bot = _FakeBot(post_result=_sent_rich_photo_json())

        msg = await rich.send_rich_message(  # type: ignore[arg-type]
            bot, 449, "**Status**\n", photo=photo_bytes
        )

        endpoint, data = bot.posts[0]
        assert endpoint == "sendRichMessage"
        assert data["rich_message"] == {
            "markdown": (
                "**Status**\n\n"
                "![](tg://photo?id=terminal_screenshot)"
            ),
            "media": [
                {
                    "id": "terminal_screenshot",
                    "media": {
                        "type": "photo",
                        "media": "attach://terminal_screenshot",
                    },
                }
            ],
        }
        upload = data["terminal_screenshot"]
        assert isinstance(upload, InputFile)
        assert upload.input_file_content == photo_bytes
        assert upload.filename == "terminal_screenshot.png"
        assert upload.attach_name is None
        assert rich.extract_rich_photo_file_id(msg) == "photo-large"

    @pytest.mark.asyncio
    async def test_send_reuses_photo_file_id_without_upload(self) -> None:
        bot = _FakeBot(post_result=_sent_message_json())

        await rich.send_rich_message(  # type: ignore[arg-type]
            bot, 449, "Status", photo="existing-photo-file-id"
        )

        data = bot.posts[0][1]
        assert "terminal_screenshot" not in data
        assert data["rich_message"]["media"] == [
            {
                "id": "terminal_screenshot",
                "media": {
                    "type": "photo",
                    "media": "existing-photo-file-id",
                },
            }
        ]
        assert data["rich_message"]["markdown"].endswith(
            "\n\n![](tg://photo?id=terminal_screenshot)"
        )

    @pytest.mark.asyncio
    async def test_photo_anchor_places_media_before_service_tail(self) -> None:
        bot = _FakeBot(post_result=_sent_message_json())
        markdown = (
            "answer\n\n"
            f"{rich.RICH_PHOTO_ANCHOR}\n\n"
            "context: 42%\n\n─── фон ───"
        )

        await rich.send_rich_message(  # type: ignore[arg-type]
            bot, 449, markdown, photo="existing-photo-file-id"
        )

        rendered = bot.posts[0][1]["rich_message"]["markdown"]
        assert rich.RICH_PHOTO_ANCHOR not in rendered
        assert rendered.index("![](tg://photo?id=terminal_screenshot)") < (
            rendered.index("context: 42%")
        )

    @pytest.mark.asyncio
    async def test_photo_anchor_is_invisible_without_photo(self) -> None:
        bot = _FakeBot(post_result=_sent_message_json())

        await rich.send_rich_message(  # type: ignore[arg-type]
            bot, 449, f"answer{rich.RICH_PHOTO_ANCHOR}context"
        )

        assert bot.posts[0][1]["rich_message"]["markdown"] == "answercontext"

    @pytest.mark.asyncio
    async def test_send_forwards_explicit_disable_notification_only(self) -> None:
        quiet_bot = _FakeBot(post_result=_sent_message_json())
        default_bot = _FakeBot(post_result=_sent_message_json())

        await rich.send_rich_message(  # type: ignore[arg-type]
            quiet_bot, 449, "Status", disable_notification=True
        )
        await rich.send_rich_message(  # type: ignore[arg-type]
            default_bot, 449, "Status"
        )

        assert quiet_bot.posts[0][1]["disable_notification"] is True
        assert "disable_notification" not in default_bot.posts[0][1]

    @pytest.mark.asyncio
    async def test_edit_accepts_true_result_with_photo_upload(self) -> None:
        from telegram import InputFile

        bot = _FakeBot(post_result=True)

        result = await rich.edit_rich_message(  # type: ignore[arg-type]
            bot, 449, 7, "Status", photo=b"photo"
        )

        assert result is None
        endpoint, data = bot.posts[0]
        assert endpoint == "editMessageText"
        assert data["message_id"] == 7
        assert isinstance(data["terminal_screenshot"], InputFile)
        assert data["rich_message"]["media"][0]["media"]["media"] == (
            "attach://terminal_screenshot"
        )

    @pytest.mark.asyncio
    async def test_edit_returns_message_for_new_photo_file_id(self) -> None:
        bot = _FakeBot(post_result=_sent_rich_photo_json())

        msg = await rich.edit_rich_message(  # type: ignore[arg-type]
            bot, 449, 7, "Status", photo=b"new photo"
        )

        assert msg is not None and msg.message_id == 42
        assert rich.extract_rich_photo_file_id(msg) == "photo-large"

    def test_extract_file_id_from_raw_rich_message(self) -> None:
        raw = _sent_rich_photo_json()["rich_message"]

        assert rich.extract_rich_photo_file_id(raw) == "photo-large"

    def test_extract_file_id_handles_nested_and_missing_media(self) -> None:
        raw = {
            "blocks": [
                {
                    "type": "collage",
                    "blocks": [
                        {
                            "type": "photo",
                            "photo": [
                                {
                                    "file_id": "nested",
                                    "width": 320,
                                    "height": 200,
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        assert rich.extract_rich_photo_file_id(raw) == "nested"
        assert rich.extract_rich_photo_file_id({"blocks": []}) is None
        assert rich.extract_rich_photo_file_id(True) is None


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
    async def test_multiline_shell_command_stays_rich_and_becomes_code(
        self, rich_on: None
    ) -> None:
        bot = _FakeBot(post_result=_sent_message_json())
        text = (
            "**Deploy:**\n\n```bash\n"
            "git fetch origin\n"
            "git pull --ff-only origin main\n"
            "```"
        )

        await message_sender.safe_send(bot, 449, text)  # type: ignore[arg-type]

        assert len(bot.posts) == 1
        markdown = bot.posts[0][1]["rich_message"]["markdown"]
        assert markdown.startswith("**Deploy:**")
        assert (
            "<code>git fetch origin\ngit pull --ff-only origin main</code>" in markdown
        )
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiline_non_shell_block_preserves_rich_formatting(
        self, rich_on: None
    ) -> None:
        bot = _FakeBot(post_result=_sent_message_json())
        text = "**Example:**\n\n```python\nimport os\nprint(os.getcwd())\n```"

        await message_sender.safe_send(bot, 449, text)  # type: ignore[arg-type]

        assert len(bot.posts) == 1
        bot.send_message.assert_not_called()

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

    @pytest.mark.asyncio
    async def test_multiline_shell_command_edit_stays_rich(self, rich_on: None) -> None:
        bot = _FakeBot(post_result=True)
        bot.edit_message_text = AsyncMock()  # type: ignore[attr-defined]
        target = _FakeMessage(bot)

        await message_sender.safe_edit(
            target, "```sh\nsystemctl daemon-reload\nsystemctl restart ccbot\n```"
        )

        assert len(bot.posts) == 1
        markdown = bot.posts[0][1]["rich_message"]["markdown"]
        assert markdown == (
            "<code>systemctl daemon-reload\nsystemctl restart ccbot</code>"
        )
        bot.edit_message_text.assert_not_called()  # type: ignore[attr-defined]


class TestTryRichEditRaw:
    @pytest.mark.asyncio
    async def test_edit_lands(self, rich_on: None) -> None:
        bot = _FakeBot(post_result=True)
        ok = await message_sender.try_rich_edit(bot, 449, 7, "a < b")
        assert ok is True
        assert bot.posts == [
            (
                "editMessageText",
                {
                    "chat_id": 449,
                    "message_id": 7,
                    "rich_message": {"markdown": "a &lt; b"},
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_not_modified_is_success(self, rich_on: None) -> None:
        from telegram.error import BadRequest

        bot = _FakeBot(post_error=BadRequest("Message is not modified: blah"))
        assert await message_sender.try_rich_edit(bot, 449, 7, "same") is True

    @pytest.mark.asyncio
    async def test_error_means_fallback(self, rich_on: None) -> None:
        bot = _FakeBot(post_error=RuntimeError("boom"))
        assert await message_sender.try_rich_edit(bot, 449, 7, "x") is False

    @pytest.mark.asyncio
    async def test_rich_off_means_fallback(self, rich_off: None) -> None:
        bot = _FakeBot(post_result=True)
        assert await message_sender.try_rich_edit(bot, 449, 7, "x") is False
        assert bot.posts == []

    @pytest.mark.asyncio
    async def test_retry_after_propagates(self, rich_on: None) -> None:
        from telegram.error import RetryAfter

        bot = _FakeBot(post_error=RetryAfter(3))
        with pytest.raises(RetryAfter):
            await message_sender.try_rich_edit(bot, 449, 7, "x")


class TestCardEditRichPath:
    """The live card's in-place edits must go rich-first — otherwise the
    first edit after a rich _send_card visibly downgrades the card to
    MarkdownV2 (tables/headings/<details> lose native rendering)."""

    @pytest.mark.asyncio
    async def test_edit_card_uses_rich(self, rich_on: None) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        from ccbot.handlers import notifications
        from ccbot.handlers.card_model import CardState

        bot = _FakeBot(post_result=True)
        bot.edit_message_text = AsyncMock()  # type: ignore[attr-defined]
        state = CardState(msg_id=7)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("x", callback_data="y")]])
        ok = await notifications._edit_card(  # pyright: ignore[reportPrivateUsage]
            bot,  # type: ignore[arg-type]
            449,
            state,
            text="| a | b |\n|---|---|\n| 1 | 2 |",
            reply_markup=markup,
        )
        assert ok is True
        assert len(bot.posts) == 1
        endpoint, data = bot.posts[0]
        assert endpoint == "editMessageText"
        # Table cells get the <sub> font-shrink wrap on the rich path.
        assert data["rich_message"]["markdown"].startswith("| <sub>a</sub> |")
        assert data["reply_markup"] is markup
        bot.edit_message_text.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_edit_card_falls_back_to_markdownv2(self, rich_on: None) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        from ccbot.handlers import notifications
        from ccbot.handlers.card_model import CardState

        bot = _FakeBot(post_error=RuntimeError("boom"))
        bot.edit_message_text = AsyncMock()  # type: ignore[attr-defined]
        state = CardState(msg_id=7)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("x", callback_data="y")]])
        ok = await notifications._edit_card(  # pyright: ignore[reportPrivateUsage]
            bot,  # type: ignore[arg-type]
            449,
            state,
            text="hello",
            reply_markup=markup,
        )
        assert ok is True
        bot.edit_message_text.assert_called_once()  # type: ignore[attr-defined]
        assert (
            bot.edit_message_text.call_args.kwargs["parse_mode"]  # type: ignore[attr-defined]
            == "MarkdownV2"
        )
