"""E2E regression (touches bug A6): switcher tap → active flip + history render.

Tapping a session button (callback data ``sw:<sid>``) must:
  * flip ``active_sessions`` to the tapped session, and
  * repaint the carrier as that session's live card seeded from JSONL.

The A6 angle: the seeded card's footer page counter must reflect the real
multi-turn transcript (``card_page_info`` total > 1), NOT collapse to ``1/1``.
We seed a 3-turn JSONL for the target session and assert both the flip and the
multi-page count after the tap goes through the real ``callback_handler`` →
``switcher.handle`` → ``paint_card_on_carrier`` chain.
"""

from __future__ import annotations

import pytest

from ccbot.bot.callbacks import callback_handler
from ccbot.handlers import notifications
from ccbot.handlers.notifications import card_page_info
from ccbot.session import session_manager

from harness import (
    USER_ID,
    FakeCallbackQuery,
    FakeUpdate,
    FakeUser,
    append_jsonl,
    assistant_turn,
    make_jsonl_path,
    seed_session,
    user_turn,
    write_session_map,
)

WORKDIR_A = "/tmp/sessA"
WORKDIR_B = "/tmp/sessB"
SID_A = "55555555-5555-5555-5555-555555555555"
SID_B = "66666666-6666-6666-6666-666666666666"


def _ctx(fake_bot):
    class _Ctx:
        bot = fake_bot
        user_data: dict = {}

    return _Ctx()


@pytest.mark.asyncio
async def test_switcher_tap_flips_active_and_renders_multipage(
    fake_tmux, fake_bot, projects_path, no_card_lag
):
    from ccbot.config import config

    # Session A active on @100; session B background on @200.
    fake_tmux.add_window("@100", name="sessA", cwd=WORKDIR_A, pane="idle\n")
    fake_tmux.add_window("@200", name="sessB", cwd=WORKDIR_B, pane="idle\n")
    seed_session(
        session_manager,
        sid="aaaaaaaa",
        name="sessA",
        window_id="@100",
        workdir=WORKDIR_A,
        claude_session_id=SID_A,
        active_for=USER_ID,
    )
    seed_session(
        session_manager,
        sid="bbbbbbbb",
        name="sessB",
        window_id="@200",
        workdir=WORKDIR_B,
        claude_session_id=SID_B,
    )
    # session_map so _seed_events_from_jsonl resolves B's transcript path.
    write_session_map(
        config.session_map_file,
        window_id="@200",
        claude_session_id=SID_B,
        cwd=WORKDIR_B,
    )

    # Seed B's JSONL with THREE completed turns → three turn-based pages.
    jsonl_b = make_jsonl_path(projects_path, WORKDIR_B, SID_B)
    append_jsonl(jsonl_b, user_turn("q1", cwd=WORKDIR_B))
    append_jsonl(jsonl_b, assistant_turn("answer one"))
    append_jsonl(jsonl_b, user_turn("q2", cwd=WORKDIR_B))
    append_jsonl(jsonl_b, assistant_turn("answer two"))
    append_jsonl(jsonl_b, user_turn("q3", cwd=WORKDIR_B))
    append_jsonl(jsonl_b, assistant_turn("answer three"))

    # The carrier is the message the switcher button lives on (msg 8000).
    user = FakeUser(USER_ID)
    query = FakeCallbackQuery(
        data="sw:bbbbbbbb",
        user=user,
        message_id=8000,
        chat_id=USER_ID,
        bot=fake_bot,
    )
    update = FakeUpdate(user=user, callback_query=query)

    await callback_handler(update, _ctx(fake_bot))

    # Active session flipped to B.
    active = session_manager.get_active_session(USER_ID)
    assert active is not None and active.id == "bbbbbbbb"

    # The carrier (msg 8000) was claimed + painted as B's live card.
    assert fake_bot.edit_message_text.call_count >= 1
    assert any(e["message_id"] == 8000 for e in fake_bot.edits)

    # A6: the seeded card's page counter must NOT collapse to 1/1 — the
    # 3-turn transcript yields >1 turn-based pages.
    state = notifications._cards[(USER_ID, "bbbbbbbb")]
    _idx, total = card_page_info(state, USER_ID)
    assert total > 1, f"page counter collapsed to {total}/1 after switcher tap"

    # The query was acknowledged.
    assert query.answers, "switcher tap did not answer the callback query"


@pytest.mark.asyncio
async def test_switcher_tap_on_dead_session_alerts(fake_tmux, fake_bot):
    # Tapping a session that has been archived must alert + not flip active.
    fake_tmux.add_window("@100", name="sessA", cwd=WORKDIR_A, pane="idle\n")
    seed_session(
        session_manager,
        sid="aaaaaaaa",
        name="sessA",
        window_id="@100",
        workdir=WORKDIR_A,
        claude_session_id=SID_A,
        active_for=USER_ID,
    )
    seed_session(
        session_manager,
        sid="bbbbbbbb",
        name="sessB",
        window_id="",
        workdir=WORKDIR_B,
        claude_session_id=SID_B,
        state="archived",
    )

    user = FakeUser(USER_ID)
    query = FakeCallbackQuery(
        data="sw:bbbbbbbb",
        user=user,
        message_id=8000,
        chat_id=USER_ID,
        bot=fake_bot,
    )
    update = FakeUpdate(user=user, callback_query=query)

    await callback_handler(update, _ctx(fake_bot))

    # Active stays A; alert shown.
    active = session_manager.get_active_session(USER_ID)
    assert active is not None and active.id == "aaaaaaaa"
    assert query.answers and query.answers[-1][1] is True  # show_alert=True
