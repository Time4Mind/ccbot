"""Short replies bypass the default editor without changing delivery or intent."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot._messages_preprocessing import prepare_request_for_dispatch
from ccbot.request_preprocessing import (
    DEFAULT_PREPROCESSING_INSTRUCTION,
    PromptPreprocessor,
    recover_pending_preprocessing,
)
from ccbot.session_models import Session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["Да", "НЕТ!", "Ок.", "окей", "Ага", "угу", "Готово", "OK"]
)
async def test_short_reply_does_not_create_editor_session(text: str) -> None:
    factory = AsyncMock(side_effect=AssertionError("editor must not be started"))
    processor = PromptPreprocessor(session_factory=factory)

    assert await processor.process(text) == text
    factory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "instruction"),
    [
        ("Да?", ""),
        ("Да...", ""),
        ("Да, но только на стенде", ""),
        ("Нет, не удаляй", ""),
        ("Ну да", ""),
        ("Обнови мне поищу.", ""),
        ("Да", "Переведи ответ на английский"),
    ],
)
async def test_other_inputs_and_custom_instructions_still_use_editor(
    text: str, instruction: str
) -> None:
    session = SimpleNamespace(rewrite=AsyncMock(return_value="result"))
    factory = AsyncMock(return_value=session)
    processor = PromptPreprocessor(session_factory=factory)

    assert await processor.process(text, instruction=instruction) == "result"
    session.rewrite.assert_awaited_once_with(
        instruction or DEFAULT_PREPROCESSING_INSTRUCTION, text
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "voice"])
async def test_short_reply_dispatch_skips_editor_and_preprocessing_state(
    kind: str,
) -> None:
    manager = MagicMock()
    manager.get_user_settings.return_value = {
        "preprocessing_mode": "all",
        "preprocessing_instruction": "",
    }
    update = SimpleNamespace(message=SimpleNamespace(message_id=17))
    editor = AsyncMock(side_effect=AssertionError("editor must not be called"))
    with (
        patch("ccbot.bot._messages_preprocessing.session_manager", manager),
        patch("ccbot.bot._messages_preprocessing.prompt_preprocessor.process", editor),
        patch("ccbot.input_recovery.input_recovery.current", return_value=None),
    ):
        prepared = await prepare_request_for_dispatch(
            update, MagicMock(), 42, "@A", "Да", input_kind=kind
        )
        prepared.confirm_delivery()

    assert prepared.text == "Да"
    assert prepared.preprocessed is False
    assert prepared.record is None
    editor.assert_not_awaited()
    manager.find_session_by_window.assert_not_called()
    manager.save_state.assert_not_called()


@pytest.mark.asyncio
async def test_pending_short_reply_recovery_does_not_call_editor_or_mark_rewrite() -> (
    None
):
    target = Session(
        id="short-reply",
        name="A",
        window_id="@A",
        pending_preprocessing=[
            {
                "original": "Да",
                "prepared": "",
                "instruction": "",
                "state": "preprocessing",
            }
        ],
    )
    manager = MagicMock()
    manager.sessions = {target.id: target}
    manager.send_to_window = AsyncMock(return_value=(True, "ok"))
    editor = SimpleNamespace(
        process=AsyncMock(side_effect=AssertionError("editor must not be called"))
    )

    assert await recover_pending_preprocessing(manager=manager, processor=editor) == 1
    manager.send_to_window.assert_awaited_once_with("@A", "Да")
    editor.process.assert_not_awaited()
    assert not target.was_preprocessed_prompt("Да")
    assert target.pending_preprocessing == []


def test_builtin_instruction_stays_compact() -> None:
    assert len(DEFAULT_PREPROCESSING_INSTRUCTION.split()) <= 170
