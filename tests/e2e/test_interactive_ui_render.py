"""E2E regression (bugs A5 + ResumeSummary): interactive-UI keyboard render.

``status_polling.update_status_message`` captures the pane each poll. When it
detects an interactive UI on the ACTIVE session, it flips the live card into
kb-mode via ``enter_kb_mode``, attaching the arrow/Enter/Esc keyboard built by
``build_kb_mode_keyboard``.

Two regressions are covered end-to-end through that real poll path:
  * A5 — a tall MULTI-question AskUserQuestion where both the ``☐`` header and
    the ``Enter to select`` footer scrolled off the capture (only the ``❯ N.``
    cursor + numbered options remain) still classifies + renders a keyboard.
  * ResumeSummary — the ``claude --resume`` "Resume from summary" prompt
    renders a keyboard and stashes the pending interactive UI.

We assert an ``InlineKeyboardMarkup`` carrying the kb-mode keys reached
Telegram, and that the card recorded kb-mode for the prompt.
"""

from __future__ import annotations

import pytest
from telegram import InlineKeyboardMarkup

from ccbot.handlers import notifications
from ccbot.handlers.notifications import has_pending_kb
from ccbot.handlers.status_polling import update_status_message
from ccbot.session import session_manager

from harness import USER_ID, seed_session

WINDOW_ID = "@100"
WORKDIR = "/tmp/proj"
CLAUDE_SID = "77777777-7777-7777-7777-777777777777"

# A5: multi-question AskUserQuestion, header + footer scrolled off — only the
# bottom-less ``❯ N.`` cursor + numbered options visible.
MULTIQUESTION_PANE = (
    "      Which migration strategy should we adopt for the auth service? (1/2)\n"
    "\n"
    "❯ 1. Incremental — migrate the service module-by-module across several\n"
    "       PRs, keeping both code paths alive behind a feature flag until\n"
    "       we reach parity and can delete the old path\n"
    "  2. Big-bang — cut over everything in a single release after a\n"
    "       long-lived branch lands, accepting one riskier merge in exchange\n"
    "       for never running two implementations at once\n"
    "  3. Strangler-fig — stand the new service up alongside the old one and\n"
    "       route traffic across at the edge, retiring routes as they move\n"
    "  4. Let me describe a different approach\n"
)

# ResumeSummary: the claude --resume numbered single-select prompt.
RESUME_SUMMARY_PANE = (
    "This session is 11h 34m old and 258.3k tokens.\n"
    "\n"
    "Resuming the full session will consume a substantial portion of your "
    "usage limits. We recommend resuming from a summary.\n"
    "\n"
    "❯ 1. Resume from summary (recommended)\n"
    "  2. Resume full session as-is\n"
    "  3. Don't ask me again\n"
    "\n"
    "Enter to confirm · Esc to cancel\n"
)


def _kb_labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _captured_keyboards(fake_bot) -> list[InlineKeyboardMarkup]:
    """Every InlineKeyboardMarkup that reached send_message / edit_message_text."""
    out: list[InlineKeyboardMarkup] = []
    for m in fake_bot.sent_messages:
        if isinstance(m.reply_markup, InlineKeyboardMarkup):
            out.append(m.reply_markup)
    for e in fake_bot.edits:
        rm = e.get("reply_markup")
        if isinstance(rm, InlineKeyboardMarkup):
            out.append(rm)
    return out


def _seed_active(sess_state: str = "active") -> None:
    seed_session(
        session_manager,
        sid="eeee7777",
        name="proj",
        window_id=WINDOW_ID,
        workdir=WORKDIR,
        claude_session_id=CLAUDE_SID,
        active_for=USER_ID,
        state=sess_state,
    )


@pytest.mark.asyncio
async def test_multiquestion_askuser_renders_keyboard(fake_tmux, fake_bot, no_card_lag):
    fake_tmux.add_window(WINDOW_ID, name="proj", cwd=WORKDIR, pane=MULTIQUESTION_PANE)
    _seed_active()

    await update_status_message(fake_bot, USER_ID, WINDOW_ID)

    keyboards = _captured_keyboards(fake_bot)
    assert keyboards, "no inline keyboard rendered for the AskUserQuestion prompt"
    labels = _kb_labels(keyboards[-1])
    # kb-mode grid carries arrows + Enter + Esc.
    assert any("↑" in label for label in labels)
    assert any("Enter" in label for label in labels)
    assert any("Esc" in label for label in labels)

    # Card recorded kb-mode (pending prompt) for this session.
    has_prompt, in_kb = has_pending_kb(USER_ID, "eeee7777")
    assert has_prompt and in_kb
    state = notifications._cards[(USER_ID, "eeee7777")]
    assert state.kb_ui_name == "AskUserQuestion"


@pytest.mark.asyncio
async def test_resume_summary_renders_keyboard_and_stashes(
    fake_tmux, fake_bot, no_card_lag
):
    fake_tmux.add_window(WINDOW_ID, name="proj", cwd=WORKDIR, pane=RESUME_SUMMARY_PANE)
    _seed_active()

    await update_status_message(fake_bot, USER_ID, WINDOW_ID)

    keyboards = _captured_keyboards(fake_bot)
    assert keyboards, "no inline keyboard rendered for the ResumeSummary prompt"
    labels = _kb_labels(keyboards[-1])
    assert any("Enter" in label for label in labels)
    assert any("Esc" in label for label in labels)

    has_prompt, in_kb = has_pending_kb(USER_ID, "eeee7777")
    assert has_prompt and in_kb
    state = notifications._cards[(USER_ID, "eeee7777")]
    assert state.kb_ui_name == "ResumeSummary"
    # The prompt content is stashed on the card for re-render / Resume.
    assert "Resume from summary" in state.kb_prompt
