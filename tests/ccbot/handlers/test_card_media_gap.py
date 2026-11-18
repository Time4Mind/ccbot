"""An agent's missing-voice context is not a user row in the Telegram card."""

from ccbot.handlers.card_events import _build_event
from ccbot.handlers.card_seed import _events_from_parsed_entries
from ccbot.handlers.card_types import CardState, PendingPrompt
from ccbot.handlers.card_updates import _apply_preprocessing_marker
from ccbot.session_models import Session
from ccbot.session_monitor import NewMessage
from ccbot.transcript_parser import TranscriptParser

PROMPT = "Проверь карточку.\n\nСохрани второй абзац."
GAP = (
    "[Сообщение ccbot: предыдущее голосовое не получено агентом "
    "из-за ошибки загрузки, запросы №17350, №17356. "
    "Не восстанавливай его содержание по догадке.]"
)
TEXT = f"{GAP}\n\n{PROMPT}"


def test_live_card_uses_prompt_text_and_matches_its_receipt():
    sess = Session(id="s1", name="Demo")
    pending = PendingPrompt(request_id="42", text=PROMPT, preprocessed=True)
    state = CardState(
        pending_prompts=[pending],
        pending_request_sequences=[(42, 7)],
    )
    raw = NewMessage(
        session_id=sess.id,
        text=TEXT,
        is_complete=True,
        content_type="text",
        role="user",
    )
    event = _build_event(raw)
    _apply_preprocessing_marker(sess, state, event, raw.text)
    assert event.type == "user_msg"
    assert event.text == PROMPT
    assert raw.text == TEXT  # Exact transport text remains available.
    assert event.user_icon == "👤💻"
    assert state.pending_prompts == []
    assert state.active_turn_sequence == 7


def test_rebuilt_card_keeps_old_record_text_out_of_user_row():
    rows = [
        {
            "type": "response_item",
            "ordinal": 1,
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": TEXT}],
            },
        }
    ]
    parsed, _ = TranscriptParser.parse_entries(rows)
    sess = Session(id="s1", name="Demo")
    sess.remember_preprocessed_prompt(PROMPT)
    result = _events_from_parsed_entries(sess, parsed, 5)
    assert [(r.type, r.text, r.user_icon) for r in result] == [
        ("user_msg", PROMPT, "👤💻")
    ]
    assert parsed[0].text == TEXT


def test_literal_mentions_in_message_and_assistant_text_stay_visible():
    for role, text in (
        ("user", f"Цитирую старую подсказку:\n\n{TEXT}"),
        ("assistant", TEXT),
    ):
        msg = NewMessage(
            session_id="s1", text=text, is_complete=True, content_type="text", role=role
        )
        assert _build_event(msg).text == text
