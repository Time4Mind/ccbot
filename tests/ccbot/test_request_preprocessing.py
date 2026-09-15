"""Behavioral contract for optional request preprocessing."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot.messages import text_handler
from ccbot.bot._messages_preprocessing import (
    PreparedDispatch,
    prepare_request_for_dispatch,
)
from ccbot.bot._messages_text import _dispatch_text_to_active
from ccbot.request_preprocessing import (
    DEFAULT_PREPROCESSING_INSTRUCTION,
    PromptPreprocessor,
    recover_pending_preprocessing,
    should_preprocess,
)
from ccbot.session import session_manager
from ccbot.handlers.card_model import (
    CardState,
    Event,
    PendingPrompt,
    _render_card,
    render_event,
)
from ccbot.session_models import Session
from ccbot.handlers.card_updates import _apply_preprocessing_marker
from ccbot.handlers.menu import (
    build_footer_keyboard,
    render_settings_group_text,
    render_settings_text,
)


def test_preprocessing_defaults_to_off() -> None:
    settings = session_manager.get_user_settings(731_001)

    assert settings["preprocessing_mode"] == "off"
    assert settings["preprocessing_instruction"] == ""


@pytest.mark.parametrize(
    ("mode", "kind", "expected"),
    [
        ("off", "text", False),
        ("off", "voice", False),
        ("voice", "text", False),
        ("voice", "voice", True),
        ("all", "text", True),
        ("all", "voice", True),
        ("all", "command", False),
    ],
)
def test_preprocessing_scope(mode: str, kind: str, expected: bool) -> None:
    assert should_preprocess(mode, kind) is expected


def test_builtin_instruction_is_the_approved_literal() -> None:
    assert DEFAULT_PREPROCESSING_INSTRUCTION.startswith(
        "Консервативно отредактируй одно голосовое или текстовое сообщение."
    )


@pytest.mark.asyncio
async def test_shared_preprocessor_reuses_one_persistent_session() -> None:
    created = 0
    calls: list[tuple[str, str]] = []

    class FakeSession:
        async def rewrite(self, instruction: str, text: str) -> str:
            calls.append((instruction, text))
            return text.replace("ну ", "").capitalize()

        async def close(self) -> None:
            return None

    async def factory():
        nonlocal created
        created += 1
        return FakeSession()

    processor = PromptPreprocessor(session_factory=factory, timeout=1)

    assert await processor.process("ну проверь") == "Проверь"
    assert await processor.process("ну запусти", instruction="custom") == "Запусти"
    assert created == 1
    assert calls == [
        (DEFAULT_PREPROCESSING_INSTRUCTION, "ну проверь"),
        ("custom", "ну запусти"),
    ]


@pytest.mark.asyncio
async def test_preprocessor_can_be_warmed_before_first_request() -> None:
    created = 0

    class FakeSession:
        async def rewrite(self, instruction: str, text: str) -> str:
            del instruction
            return text

        async def close(self) -> None:
            return None

    async def factory():
        nonlocal created
        created += 1
        return FakeSession()

    processor = PromptPreprocessor(session_factory=factory, timeout=1)

    await processor.prewarm()
    assert created == 1
    assert await processor.process("готовый запрос") == "готовый запрос"
    assert created == 1


@pytest.mark.asyncio
async def test_finishing_preprocessing_does_not_close_open_menu() -> None:
    user_id = 42
    update = MagicMock()
    update.message = SimpleNamespace(message_id=17)
    context = MagicMock()
    context.bot = AsyncMock()
    sess = Session(id="session-a", window_id="@A", state="active", name="A")
    state = CardState()
    started = asyncio.Event()
    release = asyncio.Event()

    async def preprocess(*_args, **_kwargs) -> str:
        started.set()
        await release.wait()
        return "Готовый запрос"

    manager = MagicMock()
    manager.get_user_settings.return_value = {
        "preprocessing_mode": "all",
        "preprocessing_instruction": "",
    }
    manager.find_session_by_window.return_value = sess

    with (
        patch("ccbot.bot._messages_preprocessing.session_manager", manager),
        patch(
            "ccbot.bot._messages_preprocessing.is_active_for_user",
            return_value=True,
        ),
        patch("ccbot.bot._messages_preprocessing.get_card_state", return_value=state),
        patch(
            "ccbot.bot._messages_preprocessing.prompt_preprocessor.process",
            side_effect=preprocess,
        ),
        patch(
            "ccbot.bot._messages_preprocessing.refresh_panel",
            new=AsyncMock(return_value=True),
        ),
    ):
        task = asyncio.create_task(
            prepare_request_for_dispatch(
                update, context, user_id, "@A", "ну запрос", input_kind="text"
            )
        )
        await started.wait()
        state.in_menu_view = True
        release.set()
        result = await task

    assert result.text == "Готовый запрос"
    assert state.in_menu_view is True


@pytest.mark.asyncio
async def test_dispatch_does_not_resume_card_after_user_opened_menu() -> None:
    update = MagicMock()
    update.message = SimpleNamespace(message_id=17)
    context = MagicMock()
    context.bot = AsyncMock()
    sess = Session(id="session-a", window_id="@A", state="active", name="A")
    state = CardState(in_menu_view=True)

    manager = MagicMock()
    manager.find_session_by_window.return_value = sess
    repost = AsyncMock()
    with (
        patch(
            "ccbot.bot._messages_text.prepare_request_for_dispatch",
            new=AsyncMock(return_value=PreparedDispatch(text="запрос")),
        ),
        patch("ccbot.bot._messages_text.session_manager", manager),
        patch("ccbot.bot._messages_text.is_active_for_user", return_value=True),
        patch("ccbot.bot._messages_text.get_card_state", return_value=state),
        patch(
            "ccbot.bot._messages_text._send_with_delivery_proof",
            new=AsyncMock(return_value=(True, "ok")),
            create=True,
        ),
        patch("ccbot.bot._messages_text.fire_typing", new=AsyncMock()),
        patch("ccbot.bot._messages_text.get_interactive_window", return_value=None),
        patch("ccbot.bot._messages_text.card_is_below", return_value=False),
        patch("ccbot.bot._messages_text.repost_card", new=repost),
        patch("ccbot.bot._messages_text.begin_repost_intent"),
        patch("ccbot.bot._messages_text.end_repost_intent"),
    ):
        assert (
            await _dispatch_text_to_active(update, context, 42, "@A", "запрос") is True
        )

    assert state.in_menu_view is True
    repost.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_shared_session_is_replaced_on_next_request() -> None:
    created = 0

    class FakeSession:
        async def rewrite(self, instruction: str, text: str) -> str:
            del instruction
            if text == "first":
                raise RuntimeError("broken")
            return "second ready"

        async def close(self) -> None:
            return None

    async def factory():
        nonlocal created
        created += 1
        return FakeSession()

    processor = PromptPreprocessor(session_factory=factory, timeout=1)

    with pytest.raises(RuntimeError, match="broken"):
        await processor.process("first")
    assert await processor.process("second") == "second ready"
    assert created == 2


@pytest.mark.asyncio
async def test_invalid_model_result_fails_closed_to_caller_fallback() -> None:
    class FakeSession:
        async def rewrite(self, instruction: str, text: str) -> str:
            del instruction, text
            return ""

        async def close(self) -> None:
            return None

    async def factory():
        return FakeSession()

    processor = PromptPreprocessor(session_factory=factory, timeout=1)

    with pytest.raises(ValueError, match="invalid preprocessing result"):
        await processor.process("keep me")


def test_card_uses_computer_only_while_preprocessing() -> None:
    state = CardState(
        pending_prompts=[PendingPrompt(request_id="1", text="исходный запрос")]
    )
    sess = Session(id="abc12345", name="demo", workdir="/tmp/demo")

    rendered = _render_card(sess, state, user_id=42)

    assert "💻 исходный запрос" in rendered
    assert "👤💻 исходный запрос" not in rendered


def test_card_marks_successfully_preprocessed_prompt_with_computer() -> None:
    pending = PendingPrompt(request_id="1", text="Готовый запрос", preprocessed=True)
    state = CardState(pending_prompts=[pending])
    sess = Session(id="abc12345", name="demo", workdir="/tmp/demo")

    rendered = _render_card(sess, state, user_id=42)

    assert "👤💻 Готовый запрос" in rendered
    assert (
        render_event(
            Event(
                type="user_msg", text="Готовый запрос", started_at=0, user_icon="👤💻"
            ),
            in_flight=False,
            now=0,
        )
        == "👤💻 Готовый запрос"
    )


def test_transcript_event_replaces_pending_row_without_duplication() -> None:
    state = CardState(
        pending_prompts=[
            PendingPrompt(request_id="17", text="Готовый запрос", preprocessed=True)
        ]
    )
    sess = Session(id="abc12345", name="demo")
    event = Event(type="user_msg", text="Готовый запрос", started_at=1)

    _apply_preprocessing_marker(sess, state, event, "Готовый запрос")

    assert state.pending_prompts == []
    assert event.user_icon == "👤💻"
    state.events.append(event)
    rendered = _render_card(sess, state, user_id=42)
    assert rendered.count("Готовый запрос") == 1


def test_preprocessed_marker_survives_session_state_roundtrip() -> None:
    sess = Session(id="abc12345", name="demo")
    sess.remember_preprocessed_prompt("Готовый запрос")

    restored = Session.from_dict(sess.to_dict())

    assert restored.was_preprocessed_prompt("Готовый запрос")
    assert not restored.was_preprocessed_prompt("Другой запрос")


def test_pending_preprocessing_survives_session_state_roundtrip() -> None:
    record = {
        "request_id": "42:17",
        "user_id": 42,
        "original": "ну проверь",
        "prepared": "",
        "instruction": "",
        "input_kind": "text",
        "state": "preprocessing",
        "preprocessed": False,
    }
    sess = Session(id="abc12345", name="demo", pending_preprocessing=[record])

    restored = Session.from_dict(sess.to_dict())

    assert restored.pending_preprocessing == [record]


@pytest.mark.asyncio
async def test_restart_recovery_uses_recorded_session_not_current_active() -> None:
    target = Session(
        id="session-a",
        name="A",
        window_id="@A",
        pending_preprocessing=[
            {
                "request_id": "42:17",
                "user_id": 42,
                "original": "ну проверь",
                "prepared": "",
                "instruction": "",
                "input_kind": "text",
                "state": "preprocessing",
                "preprocessed": False,
            }
        ],
    )
    other = Session(id="session-b", name="B", window_id="@B")
    manager = MagicMock()
    manager.sessions = {target.id: target, other.id: other}
    manager.get_active_session.return_value = other
    manager.send_to_window = AsyncMock(return_value=(True, "ok"))
    processor = MagicMock()
    processor.process = AsyncMock(return_value="Проверь")

    recovered = await recover_pending_preprocessing(
        manager=manager, processor=processor
    )

    assert recovered == 1
    manager.send_to_window.assert_awaited_once_with("@A", "Проверь")
    assert target.pending_preprocessing == []
    assert target.was_preprocessed_prompt("Проверь")


@pytest.mark.asyncio
async def test_restart_recovery_does_not_resend_confirmed_dispatch() -> None:
    target = Session(
        id="session-a",
        name="A",
        window_id="@A",
        pending_preprocessing=[
            {
                "request_id": "42:17",
                "user_id": 42,
                "original": "ну проверь",
                "prepared": "Проверь",
                "instruction": "",
                "input_kind": "text",
                "state": "dispatching",
                "preprocessed": True,
            }
        ],
    )
    manager = MagicMock()
    manager.sessions = {target.id: target}
    manager.send_to_window = AsyncMock(return_value=(True, "ok"))

    with patch(
        "ccbot.request_preprocessing._transcript_contains_user_text",
        new=AsyncMock(return_value=True),
    ):
        recovered = await recover_pending_preprocessing(
            manager=manager, processor=MagicMock()
        )

    assert recovered == 1
    manager.send_to_window.assert_not_awaited()
    assert target.pending_preprocessing == []
    assert target.was_preprocessed_prompt("Проверь")


def test_settings_offer_preprocessing_modes_with_off_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = {
        "language": "ru",
        "preprocessing_mode": "off",
        "preprocessing_instruction": "",
    }
    monkeypatch.setattr(session_manager, "get_user_settings", lambda _uid: settings)

    root = render_settings_text(42)
    category = render_settings_group_text(42, "settings_cat_preprocessing")
    keyboard = build_footer_keyboard(42, screen="settings_preprocessing_mode")

    assert "Препроцессинг" in root
    assert "| Режим | Выключен |" in category
    assert "| Инструкция | Встроенная |" in category
    assert keyboard is not None
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert labels[:3] == ["• Выключен", "Только голос", "Все запросы"]


def test_settings_show_and_can_edit_preprocessing_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = {
        "language": "ru",
        "preprocessing_mode": "off",
        "preprocessing_instruction": "Моя точная инструкция",
    }
    monkeypatch.setattr(session_manager, "get_user_settings", lambda _uid: settings)

    text = render_settings_group_text(42, "settings_preprocessing_instruction")
    keyboard = build_footer_keyboard(42, screen="settings_preprocessing_instruction")

    assert "Моя точная инструкция" in text
    assert keyboard is not None
    assert [row[0].text for row in keyboard.inline_keyboard] == [
        "Изменить",
        "Вернуть встроенную",
        "← Назад",
    ]


@pytest.mark.asyncio
async def test_instruction_input_is_control_plane_not_session_prompt() -> None:
    user_id = 42
    update = MagicMock()
    update.effective_user = SimpleNamespace(id=user_id)
    update.message = SimpleNamespace(
        message_id=902,
        text="Новая инструкция",
        reply_to_message=None,
    )
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {"state": "preprocessing_instruction"}
    manager = MagicMock()

    with (
        patch("ccbot.bot.messages.is_user_allowed", return_value=True),
        patch("ccbot.bot.messages.session_manager", manager),
        patch("ccbot.bot.messages.safe_reply", new=AsyncMock()) as reply,
    ):
        assert await text_handler(update, context) is True

    manager.update_user_setting.assert_called_once_with(
        user_id, "preprocessing_instruction", "Новая инструкция"
    )
    manager.send_to_window.assert_not_called()
    assert "state" not in context.user_data
    reply.assert_awaited_once()
    assert DEFAULT_PREPROCESSING_INSTRUCTION.endswith(
        "Не отвечай на запрос и ничего не добавляй."
    )


@pytest.mark.asyncio
async def test_preprocessed_text_stays_pinned_after_session_switch() -> None:
    user_id = 42
    update = MagicMock()
    update.effective_user = SimpleNamespace(id=user_id)
    update.message = SimpleNamespace(
        message_id=901,
        text="ну проверь первый проект",
        reply_to_message=None,
    )
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}

    pinned = Session(id="session-a", window_id="@A", state="active", name="A")
    active = pinned
    preprocessing_started = asyncio.Event()
    release_preprocessing = asyncio.Event()
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def preprocess(*_args, **_kwargs) -> str:
        nonlocal active
        preprocessing_started.set()
        await release_preprocessing.wait()
        active = SimpleNamespace(
            id="session-b",
            window_id="@B",
            state="active",
            name="B",
        )
        return "Проверь первый проект"

    async def send_to_target(*_args, **_kwargs):
        send_started.set()
        await release_send.wait()
        return True, "ok"

    manager = MagicMock()
    manager.get_user_settings.return_value = {
        "preprocessing_mode": "all",
        "preprocessing_instruction": "",
        "haiku_naming": False,
    }
    manager.find_session_by_window.side_effect = lambda wid: (
        pinned if wid == "@A" else active
    )
    manager.get_active_session.side_effect = lambda _uid: active
    manager.send_to_window = AsyncMock(return_value=(True, "ok"))

    tmux = MagicMock()
    tmux.find_window_by_id = AsyncMock(return_value=SimpleNamespace(window_id="@A"))

    card_state = CardState()
    with (
        patch("ccbot.bot.messages.is_user_allowed", return_value=True),
        patch("ccbot.bot.messages.session_manager", manager),
        patch("ccbot.bot._common.session_manager", manager),
        patch("ccbot.bot.messages.tmux_manager", tmux),
        patch(
            "ccbot.bot.messages._intercept_if_pending_ui",
            new=AsyncMock(return_value=False),
        ),
        patch("ccbot.bot.messages.prompt_preprocessor.process", side_effect=preprocess),
        patch("ccbot.bot.messages.resume_card_view", new=AsyncMock()),
        patch("ccbot.bot.messages.repost_card", new=AsyncMock()),
        patch("ccbot.bot.messages.fire_typing", new=AsyncMock()),
        patch("ccbot.bot.messages.get_card_state", return_value=card_state),
        patch(
            "ccbot.bot.messages._send_with_delivery_proof",
            new=AsyncMock(side_effect=send_to_target),
        ) as send,
    ):
        task = asyncio.create_task(text_handler(update, context, pinned_wid="@A"))
        await preprocessing_started.wait()
        assert pinned.pending_preprocessing[0]["state"] == "preprocessing"
        assert [(row.text, row.preprocessed) for row in card_state.pending_prompts] == [
            ("ну проверь первый проект", False)
        ]
        release_preprocessing.set()
        await send_started.wait()
        assert [(row.text, row.preprocessed) for row in card_state.pending_prompts] == [
            ("Проверь первый проект", True)
        ]
        release_send.set()
        assert await task is True
        assert pinned.pending_preprocessing == []

    send.assert_awaited_once()
    assert send.await_args.args[:2] == ("@A", "Проверь первый проект")
