"""ZCode model panels use the same direct-choice UI as Codex."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from ccbot.handlers.card_layout import _render_card
from ccbot.handlers.card_types import CardState
from ccbot.handlers.kb_mode import build_kb_mode_keyboard, parse_picker_menu
from ccbot.session_models import Session
from ccbot.terminal_parser import extract_interactive_content
from ccbot.zcode_runtime import navigation_key

COMPOSER = """╭─ Yolo ──────────────────────────────────────────────────────╮
│ /model                                                    │
│                                                           │
│ glm-4.7-flash zai-free | enabled                           │
╰───────────────────────────────────────────────────────────╯"""
PANEL = """┌─Models──────────────────────────────────────────────────────┐
│ > glm-4.5-flash                          Z.ai Free         │
│   glm-4.7-flash  current                 Z.ai Free         │
└───────────────────────────────────────────────────────────┘"""


def content(panel=PANEL):
    result = extract_interactive_content(panel + "\n" + COMPOSER)
    assert result and result.name == "Settings"
    return result


def test_direct_buttons_mark_current_model_instead_of_cursor():
    result = content()
    picker = parse_picker_menu(result.content, result.name)
    assert picker and picker.cursor_index == 0
    keyboard = build_kb_mode_keyboard(
        42, "@5", ui_name=result.name, prompt_content=result.content
    )
    assert [[b.text for b in row] for row in keyboard.inline_keyboard] == [
        ["glm-4.5-flash"],
        ["✓ glm-4.7-flash"],
        ["× Cancel"],
    ]
    assert [row[0].callback_data for row in keyboard.inline_keyboard] == [
        "aq:pick:0:@5",
        "aq:pick:1:@5",
        "aq:esc:@5",
    ]


def test_card_is_readable_without_terminal_frames_padding_or_code_fence():
    result = content()
    state = CardState(in_kb_mode=True, kb_prompt=result.content, kb_ui_name=result.name)
    session = Session(id="picker", name="test", window_id="@5", workdir="/tmp")
    rendered = _render_card(session, state)
    assert "Select model" in rendered
    assert "glm-4.5-flash" in rendered and "glm-4.7-flash" in rendered
    assert "Z.ai Free" in rendered
    assert not any(frame in rendered for frame in ["┌", "└", "│", "```", " " * 12])


def test_hidden_current_metadata_falls_back_to_composer_model():
    result = content(PANEL.replace("  current", "         "))
    picker = parse_picker_menu(result.content, result.name)
    assert picker and [o.selected for o in picker.options] == [False, True]


def test_current_model_outside_visible_window_does_not_mark_cursor_as_selected():
    panel = PANEL.replace("  current", "         ")
    pane = (panel + "\n" + COMPOSER).replace(
        "glm-4.7-flash zai-free", "other-model zai-free"
    )
    result = extract_interactive_content(pane)
    assert result
    picker = parse_picker_menu(result.content, result.name)
    assert picker and [o.selected for o in picker.options] == [False, False]


def test_same_model_name_on_different_providers_is_unambiguous():
    panel = PANEL.replace("glm-4.7-flash", "glm-4.5-flash")
    panel = panel.replace("Z.ai Free", "Provider A", 1)
    panel = panel.replace("Z.ai Free", "Provider B", 1)
    result = content(panel)
    picker = parse_picker_menu(result.content, result.name)
    assert picker and len({o.label for o in picker.options}) == 2
    assert "Provider A" in picker.options[0].label
    assert "Provider B" in picker.options[1].label
    assert picker.options[1].selected


def test_cancel_clears_native_command_draft_with_new_picker_content():
    assert navigation_key("Escape", PANEL + "\n" + COMPOSER) == "C-u"


def test_empty_model_results_never_become_a_fake_choice():
    panel = "┌─Models──────────────┐\n│ No matching models. │\n└─────────────────────┘"
    result = content(panel)
    assert parse_picker_menu(result.content, result.name) is None


@pytest.mark.asyncio
async def test_choice_callback_uses_live_zcode_cursor_and_confirms(monkeypatch):
    from ccbot.bot.callbacks import interactive_ui

    pane = PANEL.replace("> glm-4.5", "  glm-4.5").replace("  glm-4.7", "> glm-4.7")
    monkeypatch.setattr(
        interactive_ui, "capture_window", AsyncMock(return_value=pane + "\n" + COMPOSER)
    )
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(interactive_ui, "_send_window_key", send)
    refresh = AsyncMock()
    monkeypatch.setattr(interactive_ui, "_refresh_after_key", refresh)
    monkeypatch.setattr(interactive_ui.asyncio, "sleep", AsyncMock())
    query = SimpleNamespace(data="aq:pick:0:@5", answer=AsyncMock())
    context = SimpleNamespace(bot=SimpleNamespace())

    assert await interactive_ui.handle(query, context, SimpleNamespace(id=42))

    assert send.await_args_list == [call("@5", "Up"), call("@5", "Enter")]
    refresh.assert_awaited_once_with(context.bot, 42, "@5")
