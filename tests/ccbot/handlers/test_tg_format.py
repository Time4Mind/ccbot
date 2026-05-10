"""Tests for handlers/tg_format.split_overflow."""

import pytest

from ccbot.handlers.tg_format import (
    CODE_MAX_LINES,
    TABLE_MAX_COLS,
    split_overflow,
)


class TestSplitOverflow:
    def test_empty_passthrough(self) -> None:
        r = split_overflow("")
        assert r.text == ""
        assert r.attachments == []

    def test_short_code_inline(self) -> None:
        text = "intro\n```py\nprint(1)\n```\noutro"
        r = split_overflow(text)
        assert "```py" in r.text
        assert r.attachments == []

    def test_long_code_extracted(self) -> None:
        body = "\n".join(f"line {i}" for i in range(CODE_MAX_LINES + 50))
        text = f"head\n```py\n{body}\n```\ntail"
        r = split_overflow(text)
        assert r.attachments, "expected attachment for oversized code block"
        att = r.attachments[0]
        assert att.filename.endswith(".py")
        assert b"line 0" in att.content
        assert "more lines in attached file" in r.text

    def test_long_code_unknown_lang_uses_txt(self) -> None:
        body = "\n".join(f"l{i}" for i in range(CODE_MAX_LINES + 5))
        text = f"```\n{body}\n```"
        r = split_overflow(text)
        assert r.attachments[0].filename.endswith(".txt")

    def test_small_table_inline(self) -> None:
        table = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
        r = split_overflow(table)
        assert r.attachments == []
        assert "| a | b |" in r.text

    def test_wide_table_extracted(self) -> None:
        cols = ["x" * 30] * (TABLE_MAX_COLS + 2)
        header = "| " + " | ".join(cols) + " |"
        sep = "|" + "|".join("---" for _ in cols) + "|"
        table = header + "\n" + sep + "\n" + header + "\n"
        r = split_overflow(table)
        assert r.attachments, "expected attachment for wide table"
        assert "table-1.md" in r.attachments[0].filename

    def test_six_col_weather_table_extracted(self) -> None:
        """Regression for the bug: live-card path was bypassing split_overflow,
        so a 6-column weather forecast landed inline. The fix wires
        split_overflow into ``finalize_task`` — this test asserts the helper
        catches that exact shape."""
        table = (
            "| День | Дата | День | Ночь | Осадки | Ветер |\n"
            "|------|------|------|------|--------|-------|\n"
            "| Вс | 10 мая | +13° | +11° | дождь | 6 м/с |\n"
            "| Пн | 11 мая | +13° | +12° | дождь | 2 м/с |\n"
        )
        r = split_overflow("Forecast:\n\n" + table)
        assert len(r.attachments) == 1
        assert r.attachments[0].filename == "table-1.md"
        # Inline text loses the rows, gains a placeholder.
        assert "| Вс | 10 мая" not in r.text
        assert "table 6×" in r.text
        # The full table body is preserved in the attachment (UTF-8).
        assert "| Вс | 10 мая".encode("utf-8") in r.attachments[0].content

    @pytest.mark.parametrize(
        "lang,ext",
        [
            ("python", "py"),
            ("typescript", "ts"),
            ("rs", "rs"),
            ("yaml", "yaml"),
            ("yml", "yaml"),
            ("plaintext", "txt"),
        ],
    )
    def test_lang_to_ext(self, lang: str, ext: str) -> None:
        body = "\n".join(["x"] * (CODE_MAX_LINES + 1))
        text = f"```{lang}\n{body}\n```"
        r = split_overflow(text)
        assert r.attachments[0].filename.endswith(f".{ext}")
