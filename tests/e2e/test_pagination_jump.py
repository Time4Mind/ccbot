"""The center pagination button repaints the actual latest card page."""

from __future__ import annotations

import pytest

from ccbot.bot.callbacks import callback_handler
from ccbot.handlers import notifications
from ccbot.handlers.callback_data import CB_PG_JUMP
from ccbot.handlers.card_model import Event
from ccbot.session import session_manager

from harness import USER_ID, FakeCallbackQuery, FakeUpdate, FakeUser, seed_session


@pytest.mark.asyncio
async def test_jump_paints_tool_only_tail_with_answer_mode(fake_tmux, fake_bot):
    fake_tmux.add_window("@100", name="active", cwd="/tmp/active", pane="idle\n")
    sess = seed_session(
        session_manager,
        sid="abcd1234",
        name="active",
        window_id="@100",
        workdir="/tmp/active",
        claude_session_id="11111111-1111-1111-1111-111111111111",
        active_for=USER_ID,
    )
    session_manager.update_user_setting(USER_ID, "answer_pagination_only", True)
    state = notifications.get_card_state(USER_ID, sess)
    state.events = [
        Event(type="user_msg", text="FirstRequest", started_at=1, is_page_break=True),
        Event(type="final_text", text="AnswerPage", started_at=2),
        Event(type="user_msg", text="LatestRequest", started_at=3, is_page_break=True),
        Event(type="tool_use", text="TailTool", started_at=4, tool_name="Read"),
    ]
    state.msg_id = 8000
    state.current_page_idx = 1
    state.last_rendered = notifications._render_card(sess, state, user_id=USER_ID)
    notifications._register_msg(USER_ID, 8000, sess.id)

    user = FakeUser(USER_ID)
    query = FakeCallbackQuery(
        data=CB_PG_JUMP,
        user=user,
        message_id=8000,
        chat_id=USER_ID,
        bot=fake_bot,
    )
    await callback_handler(
        FakeUpdate(user=user, callback_query=query),
        type("Context", (), {"bot": fake_bot, "user_data": {}})(),
    )

    assert query.answers
    assert fake_bot.edits
    edit = fake_bot.edits[-1]
    assert edit["message_id"] == 8000
    assert "TailTool" in edit["text"]
    assert "AnswerPage" not in edit["text"]
    assert edit["reply_markup"].inline_keyboard[0][1].text == "3/3"
