"""Regression tests for ``render_archived_card_pages`` — the Archive →
Inspect transcript renderer.

Before this renderer, Inspect flattened every message into one page and
stripped the expandable-quote sentinels, so long thinking / tool outputs
dumped inline as an unreadable wall ("портянка"). Inspect now reuses the
live-card event pipeline: thinking + tool bodies collapse into
``<details>`` spoilers exactly like the active session card, and long
outputs are line-trimmed under a ``… (+N more lines)`` marker.

These tests lock in:
  1. A long tool output is trimmed and wrapped in an EXPANDABLE_HEADED
     sentinel (→ ``<details>`` on the rich path), not dumped inline.
  2. No event renders a live ``⏳`` elapsed marker — every archived
     event is finished, so heads carry an ``HH:MM`` timestamp instead.
  3. The final assistant text lands on the last page verbatim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ccbot import rich
from ccbot.handlers.history import render_archived_card_pages
from ccbot.session_models import Session
from ccbot.transcript_format import EXPANDABLE_HEADED_START


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _transcript(make_jsonl_entry, make_tool_use_block, make_tool_result_block) -> list:
    long_output = "\n".join(f"line {i}" for i in range(40))
    return [
        make_jsonl_entry(
            "user", "поищи промокоды в yt", timestamp="2026-05-15T17:12:00.000Z"
        ),
        make_jsonl_entry(
            "assistant",
            [{"type": "thinking", "thinking": "надо грепнуть по vault"}],
            timestamp="2026-05-15T17:12:01.000Z",
        ),
        make_jsonl_entry(
            "assistant",
            [make_tool_use_block("t1", "Bash", {"command": "grep -r промокод ."})],
            timestamp="2026-05-15T17:12:02.000Z",
        ),
        make_jsonl_entry(
            "user",
            [make_tool_result_block("t1", long_output)],
            timestamp="2026-05-15T17:12:03.000Z",
        ),
        make_jsonl_entry(
            "assistant",
            [{"type": "text", "text": "Готово, Артём. Нашёл витрину."}],
            timestamp="2026-05-15T17:12:04.000Z",
        ),
    ]


@pytest.fixture
def archived_jsonl(
    tmp_path: Path,
    monkeypatch,
    make_jsonl_entry,
    make_tool_use_block,
    make_tool_result_block,
) -> Session:
    jsonl = tmp_path / "archived.jsonl"
    _write_jsonl(
        jsonl,
        _transcript(make_jsonl_entry, make_tool_use_block, make_tool_result_block),
    )
    # The renderer resolves the transcript via build_session_file_path (bound
    # into history's namespace at import); point it at our fixture file.
    monkeypatch.setattr(
        "ccbot.handlers.history.build_session_file_path",
        lambda _sid, _cwd: jsonl,
    )
    return Session(
        id="arc1",
        name="promo codes",
        state="archived",
        workdir="/some/dir",
        claude_session_id="c-arc1",
    )


@pytest.mark.asyncio
class TestArchivedCardRender:
    async def test_returns_pages_and_event_count(self, archived_jsonl) -> None:
        result = await render_archived_card_pages(archived_jsonl, user_id=1)
        assert result is not None
        pages, total = result
        assert pages
        # user + thinking + tool_use(folded result) + final_text.
        assert total >= 4

    async def test_long_tool_output_collapses_not_inline(self, archived_jsonl) -> None:
        pages, _ = await render_archived_card_pages(archived_jsonl, user_id=1)
        full = "\n".join(pages)
        # The 40-line output must be trimmed + wrapped in a headed spoiler,
        # never dumped whole inline.
        assert EXPANDABLE_HEADED_START in full
        assert "… (+" in full and "more lines)" in full
        assert "line 39" not in full  # tail of the long output is trimmed away
        # On the rich path the sentinel becomes a real <details> block.
        assert any("<details>" in rich.to_rich_markdown(p) for p in pages)

    async def test_no_live_elapsed_marker(self, archived_jsonl) -> None:
        pages, _ = await render_archived_card_pages(archived_jsonl, user_id=1)
        full = "\n".join(pages)
        # Nothing is streaming in an archived transcript → no ``⏳`` timer,
        # heads carry an HH:MM stamp instead (rendered in local time, so
        # match the shape, not a fixed value).
        assert "⏳" not in full
        assert re.search(r"·\s\d\d:\d\d", full)

    async def test_final_text_on_last_page(self, archived_jsonl) -> None:
        pages, _ = await render_archived_card_pages(archived_jsonl, user_id=1)
        assert "Готово, Артём. Нашёл витрину." in pages[-1]

    async def test_missing_transcript_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ccbot.handlers.history.build_session_file_path",
            lambda _sid, _cwd: None,
        )
        # No claude_session_id → resolves to None immediately.
        sess = Session(id="x", name="y", state="archived", workdir="/d")
        assert await render_archived_card_pages(sess, user_id=1) is None
