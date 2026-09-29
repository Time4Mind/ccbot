"""E2E: inbound user text → tmux send_keys routing.

Drives the real ``bot.messages.text_handler`` with a synthetic Update: an
allowed user with an active session sends free text. The handler must resolve
the active session → its tmux window → ``send_to_window`` → the fake tmux's
``send_keys``. We assert the exact text landed on the active session's window
(not a background one).
"""

from __future__ import annotations

import pytest

from ccbot.bot.messages import text_handler
from ccbot.handlers.directory_browser import (
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_NAMING_DIRECTORY,
    STATE_PREPROCESSING_INSTRUCTION,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
)
from ccbot.session import session_manager

from harness import (
    USER_ID,
    FakeReplyMessage,
    FakeUpdate,
    FakeUser,
    seed_session,
)


def _ctx(fake_bot):
    """A minimal ContextTypes-like object: handlers read ``.bot`` and
    ``.user_data`` (a plain dict)."""

    class _Ctx:
        bot = fake_bot
        user_data: dict = {}

    return _Ctx()


@pytest.mark.asyncio
async def test_text_routes_to_active_session_window(fake_tmux, fake_bot):
    # Two live sessions; @100 is active, @200 is background.
    fake_tmux.add_window("@100", name="active", cwd="/tmp/active")
    fake_tmux.add_window("@200", name="bg", cwd="/tmp/bg")
    seed_session(
        session_manager,
        sid="aaaa1111",
        name="active",
        window_id="@100",
        workdir="/tmp/active",
        claude_session_id="11111111-1111-1111-1111-111111111111",
        active_for=USER_ID,
    )
    seed_session(
        session_manager,
        sid="bbbb2222",
        name="bg",
        window_id="@200",
        workdir="/tmp/bg",
        claude_session_id="22222222-2222-2222-2222-222222222222",
    )

    user = FakeUser(USER_ID)
    msg = FakeReplyMessage(
        message_id=5001, chat_id=USER_ID, bot=fake_bot, text="run the build"
    )
    update = FakeUpdate(user=user, message=msg)

    await text_handler(update, _ctx(fake_bot))

    # Exactly the active window received the text.
    routed = [(wid, txt) for wid, txt, _enter, _lit in fake_tmux.sent]
    assert ("@100", "run the build") in routed
    assert all(wid != "@200" for wid, _txt in routed)


@pytest.mark.asyncio
async def test_text_with_no_active_session_opens_dir_browser(fake_tmux, fake_bot):
    # No active session at all → handler must NOT send_keys; it opens the
    # directory browser instead (pending update retained in startup queue).
    user = FakeUser(USER_ID)
    msg = FakeReplyMessage(
        message_id=5002, chat_id=USER_ID, bot=fake_bot, text="hello there"
    )
    update = FakeUpdate(user=user, message=msg)
    ctx = _ctx(fake_bot)

    await text_handler(update, ctx)

    assert fake_tmux.sent == []
    from ccbot.startup_queue import cancel_startup_queue, pending_startup_count

    assert pending_startup_count(USER_ID) == 1
    assert cancel_startup_queue(USER_ID) == 1


@pytest.mark.asyncio
async def test_text_from_unauthorized_user_is_dropped(fake_tmux, fake_bot):
    fake_tmux.add_window("@100", name="active", cwd="/tmp/active")
    seed_session(
        session_manager,
        sid="aaaa1111",
        name="active",
        window_id="@100",
        workdir="/tmp/active",
        claude_session_id="11111111-1111-1111-1111-111111111111",
        active_for=USER_ID,
    )

    intruder = FakeUser(999999)  # not in ALLOWED_USERS
    msg = FakeReplyMessage(
        message_id=5003, chat_id=999999, bot=fake_bot, text="leak something"
    )
    update = FakeUpdate(user=intruder, message=msg)

    await text_handler(update, _ctx(fake_bot))

    # Silent drop — no send_keys, no reply.
    assert fake_tmux.sent == []
    assert fake_bot.send_message.call_count == 0


@pytest.mark.parametrize(
    "bot_state",
    [
        STATE_SELECTING_SESSION,
        STATE_SELECTING_WINDOW,
        STATE_BROWSING_DIRECTORY,
        STATE_NAMING_DIRECTORY,
        STATE_PREPROCESSING_INSTRUCTION,
    ],
)
@pytest.mark.asyncio
async def test_dot_confirms_effort_picker_even_during_bot_picker(
    fake_tmux, fake_bot, monkeypatch, bot_state
):
    from ccbot import terminal_runtime

    monkeypatch.setattr(terminal_runtime, "tmux_manager", fake_tmux)
    fake_tmux.add_window(
        "@100",
        name="active",
        pane=(
            "  Select Reasoning Level for GPT-6-Sol\n"
            "\n"
            "  1. Low\n"
            "› 2. Medium (default)\n"
            "  3. High\n"
            "\n"
            "  enter default · s session · esc back\n"
        ),
    )
    seed_session(
        session_manager,
        sid="aaaa1111",
        name="active",
        window_id="@100",
        workdir="/tmp/active",
        claude_session_id="11111111-1111-1111-1111-111111111111",
        active_for=USER_ID,
    )
    msg = FakeReplyMessage(message_id=5004, chat_id=USER_ID, bot=fake_bot, text=".")
    update = FakeUpdate(user=FakeUser(USER_ID), message=msg)
    ctx = _ctx(fake_bot)
    ctx.user_data = {STATE_KEY: bot_state}

    assert await text_handler(update, ctx) is True
    assert fake_tmux.sent == [("@100", "Enter", False, False)]


@pytest.mark.parametrize(
    "bot_state",
    [
        STATE_SELECTING_SESSION,
        STATE_SELECTING_WINDOW,
        STATE_BROWSING_DIRECTORY,
        STATE_NAMING_DIRECTORY,
        STATE_PREPROCESSING_INSTRUCTION,
    ],
)
@pytest.mark.asyncio
async def test_dot_reaches_active_session_during_bot_picker(
    fake_tmux, fake_bot, bot_state
):
    fake_tmux.add_window("@100", name="active", pane="Ready\n")
    seed_session(
        session_manager,
        sid="aaaa1111",
        name="active",
        window_id="@100",
        workdir="/tmp/active",
        claude_session_id="11111111-1111-1111-1111-111111111111",
        active_for=USER_ID,
    )
    msg = FakeReplyMessage(message_id=5005, chat_id=USER_ID, bot=fake_bot, text=".")
    update = FakeUpdate(user=FakeUser(USER_ID), message=msg)
    ctx = _ctx(fake_bot)
    ctx.user_data = {STATE_KEY: bot_state}

    assert await text_handler(update, ctx) is True
    assert ("@100", ".", True, True) in fake_tmux.sent
