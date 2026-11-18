"""The missing-voice note prepended to a request for an agent."""

import re

_INTRO = (
    "[Сообщение ccbot: предыдущее голосовое не получено агентом "
    "из-за ошибки загрузки, запросы "
)
_END = ". Не восстанавливай его содержание по догадке.]\n\n"
_PREFIX = re.compile(re.escape(_INTRO) + r"№[0-9]+(?:, №[0-9]+)*" + re.escape(_END))


def media_gap_prefix(message_ids: list[str]) -> str:
    """Format the note using the exact Telegram source message IDs."""
    return _INTRO + ", ".join(f"№{message_id}" for message_id in message_ids) + _END


def strip_media_gap_prefix(text: str) -> str:
    """Drop one exact leading bot note from a user-facing view."""
    match = _PREFIX.match(text)
    return text[match.end() :] if match else text
