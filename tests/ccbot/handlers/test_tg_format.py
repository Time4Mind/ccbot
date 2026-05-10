"""Tests for handlers/tg_format.split_overflow + pretty_pad_table."""

import pytest

from ccbot.handlers.tg_format import (
    CODE_MAX_LINES,
    TABLE_MAX_COLS,
    pretty_pad_table,
    split_overflow,
)


class TestPrettyPadTable:
    def test_aligns_ragged_cells_to_uniform_width(self) -> None:
        src = "| a | very long header | x |\n|-|-|-|\n| 1 | 2 | 3 |"
        out = pretty_pad_table(src)
        # Every line should have the same length after padding.
        widths = {len(ln) for ln in out.splitlines()}
        assert len(widths) == 1

    def test_separator_row_normalised_to_dashes(self) -> None:
        src = "| h1 | h2 |\n|::|---|\n| a | b |"
        out = pretty_pad_table(src).splitlines()
        # Second line is the separator — only `|` and `-` remain.
        assert set(out[1]) == {"|", "-"}

    def test_non_table_passes_through(self) -> None:
        src = "this is just\nplain text\nno pipes"
        assert pretty_pad_table(src) == src

    def test_single_row_passes_through(self) -> None:
        src = "| just one row |"
        assert pretty_pad_table(src) == src


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

    def test_wide_table_extracted_as_photo(self) -> None:
        cols = ["x" * 30] * (TABLE_MAX_COLS + 2)
        header = "| " + " | ".join(cols) + " |"
        sep = "|" + "|".join("---" for _ in cols) + "|"
        table = header + "\n" + sep + "\n" + header + "\n"
        r = split_overflow(table)
        assert r.attachments, "expected attachment for wide table"
        assert r.attachments[0].kind == "photo"
        assert r.attachments[0].filename.endswith(".png")

    def test_six_col_weather_table_extracted_as_photo(self) -> None:
        """Regression for the bug + image-mode upgrade: wide tables now
        come out of split_overflow as ``kind="photo"`` so the sender
        rasterises them via ``screenshot.text_to_image`` for an inline
        image instead of an .md attachment."""
        table = (
            "| День | Дата | День | Ночь | Осадки | Ветер |\n"
            "|------|------|------|------|--------|-------|\n"
            "| Вс | 10 мая | +13° | +11° | дождь | 6 м/с |\n"
            "| Пн | 11 мая | +13° | +12° | дождь | 2 м/с |\n"
        )
        r = split_overflow("Forecast:\n\n" + table)
        assert len(r.attachments) == 1
        att = r.attachments[0]
        assert att.kind == "photo"
        assert att.filename == "table-1.png"
        # Inline text loses the rows, gains a placeholder.
        assert "| Вс | 10 мая" not in r.text
        assert "table 6×" in r.text
        # The source markdown stays in `content` so the sender can
        # pretty-pad and rasterise it.
        assert "| Вс | 10 мая".encode("utf-8") in att.content

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
