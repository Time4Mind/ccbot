"""Lightweight i18n service with per-user language selection.

The translation data lives in ``ccbot.i18n_locales``. This module preserves
the original public and de-facto API: ``LANGUAGES``, ``TRANSLATIONS``, the
``_EN``/``_RU``/``_ZH`` aliases, ``get_user_lang`` and ``t``.
"""

from __future__ import annotations

from typing import Any

from .i18n_locales import EN as _EN
from .i18n_locales import RU as _RU
from .i18n_locales import ZH as _ZH
from .session import session_manager

LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("ru", "Русский"),
    ("zh", "中文"),
)

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": _EN,
    "ru": _RU,
    "zh": _ZH,
}


def get_user_lang(user_id: int) -> str:
    """Resolve the user's language code, falling back to ``en``."""
    settings = session_manager.get_user_settings(user_id)
    code = settings.get("language", "en")
    if code not in TRANSLATIONS:
        return "en"
    return code


def t(user_id: int, key: str, **fmt: Any) -> str:
    """Translate ``key`` for the user and apply optional formatting."""
    lang = get_user_lang(user_id)
    table = TRANSLATIONS.get(lang) or _EN
    template = table.get(key) or _EN.get(key) or key
    if fmt:
        try:
            return template.format(**fmt)
        except (KeyError, IndexError):
            return template
    return template
