"""Regression tests for the finalized-msg carrier protection.

The bug: after ``finalize_task`` painted a session's final answer into a
live-card message, any subsequent UI tap (≡ Menu, switcher button, …)
went through ``safe_edit`` and overwrote the answer in chat. The user
saw "ответ исчез".

Fix: ``finalize_task`` pins the rendered message_id in
``_finalized_msgs`` and ``safe_edit`` checks that pin — when a carrier
is finalized it strips the keyboard from it and sends the new view as
a fresh message instead of editing in place.

These tests cover the registry semantics + the safe_edit redirect path
with a stubbed bot.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.message_sender import safe_edit


@pytest.fixture(autouse=True)
def _clear_registry():
    notifications._finalized_msgs.clear()
    yield
    notifications._finalized_msgs.clear()


class TestRegistry:
    def test_mark_then_is_finalized(self) -> None:
        notifications.mark_msg_finalized(1, 100)
        assert notifications.is_msg_finalized(1, 100) is True

    def test_unknown_user_returns_false(self) -> None:
        assert notifications.is_msg_finalized(2, 100) is False

    def test_unknown_msg_returns_false(self) -> None:
        notifications.mark_msg_finalized(1, 100)
        assert notifications.is_msg_finalized(1, 999) is False

    def test_discard_drops_flag(self) -> None:
        notifications.mark_msg_finalized(1, 100)
        notifications.discard_finalized_msg(1, 100)
        assert notifications.is_msg_finalized(1, 100) is False

    def test_discard_empties_bucket(self) -> None:
        notifications.mark_msg_finalized(1, 100)
        notifications.discard_finalized_msg(1, 100)
        assert 1 not in notifications._finalized_msgs

    def test_bucket_cap_drops_old_entries(self) -> None:
        cap = notifications._FINALIZED_LIMIT_PER_USER
        for i in range(cap + 5):
            notifications.mark_msg_finalized(1, i)
        # Newly added stays.
        assert notifications.is_msg_finalized(1, cap + 4) is True
        # Bucket size never exceeds cap.
        assert len(notifications._finalized_msgs[1]) <= cap


class _StubBot:
    def __init__(self) -> None:
        self.edit_kb_calls: list[tuple[int, int]] = []
        self.sent: list[tuple[int, str, Any]] = []

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: Any
    ) -> None:
        assert reply_markup is None
        self.edit_kb_calls.append((chat_id, message_id))

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=999)

    async def edit_message_text(self, **kwargs: Any) -> None:
        raise AssertionError(
            "edit_message_text should not be called when carrier is "
            "finalised — safe_edit should redirect to send_message"
        )


def _build_query(bot: _StubBot, chat_id: int, msg_id: int) -> Any:
    """CallbackQuery-shaped stub good enough for safe_edit's extraction."""
    chat = SimpleNamespace(id=chat_id)
    message = SimpleNamespace(message_id=msg_id, chat=chat, _bot=bot)
    return SimpleNamespace(message=message, _bot=bot)


class TestSafeEditRedirect:
    @pytest.mark.asyncio
    async def test_finalised_msg_redirects_to_new_send(self) -> None:
        bot = _StubBot()
        notifications.mark_msg_finalized(42, 2191)
        query = _build_query(bot, chat_id=42, msg_id=2191)

        await safe_edit(query, "Menu screen")

        assert bot.edit_kb_calls == [(42, 2191)]
        assert len(bot.sent) == 1
        assert bot.sent[0][0] == 42
        # After redirect the msg is no longer pinned (we don't want to
        # re-strip its kb every time a callback fires).
        assert notifications.is_msg_finalized(42, 2191) is False

    @pytest.mark.asyncio
    async def test_non_finalised_msg_falls_through(self) -> None:
        bot = _StubBot()
        # Capture edit_message_text invocations instead of raising.
        bot.edit_message_text_calls: list[dict[str, Any]] = []  # type: ignore[attr-defined]

        async def _capture(**kwargs: Any) -> None:
            bot.edit_message_text_calls.append(kwargs)  # type: ignore[attr-defined]

        bot.edit_message_text = _capture  # type: ignore[assignment]
        query = _build_query(bot, chat_id=42, msg_id=2200)

        await safe_edit(query, "Settings screen")

        assert bot.sent == []
        assert bot.edit_kb_calls == []
        # The fall-through path edits in place.
        assert bot.edit_message_text_calls  # type: ignore[attr-defined]
        assert (
            bot.edit_message_text_calls[0]["message_id"] == 2200  # type: ignore[attr-defined]
        )
