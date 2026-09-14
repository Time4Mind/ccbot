"""Rich-message file buttons backed by short callback tokens."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .handlers.callback_data import CB_FILE_SEND

FILE_CALLBACK_PREFIX = CB_FILE_SEND
_REGISTRY_LIMIT = 2000
_file_paths: dict[str, Path] = {}

_FENCED_CODE_RE = re.compile(r"```[\s\S]*?(?:```|$)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MARKDOWN_LOCAL_LINK_RE = re.compile(r"\[[^\]\n]*\]\(<?(/[^)>\n]+)>?\)")
_BARE_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w`])(/[^\s`<>\[\]{}()]+)")
_TRAILING_PROSE = ".,;:!?"


def _register(path: Path) -> str:
    token = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:20]
    if token not in _file_paths and len(_file_paths) >= _REGISTRY_LIMIT:
        for old_token in list(_file_paths)[: max(1, _REGISTRY_LIMIT // 10)]:
            _file_paths.pop(old_token, None)
    _file_paths[token] = path
    return token


def resolve_file_button(token: str) -> Path | None:
    """Resolve a button token and revalidate that it still names a file."""
    path = _file_paths.get(token)
    if path is None or not path.is_file():
        return None
    return path


def _button_for_path(raw_path: str) -> str | None:
    trailing = ""
    candidate = raw_path
    while candidate and candidate[-1] in _TRAILING_PROSE:
        trailing = candidate[-1] + trailing
        candidate = candidate[:-1]
    try:
        path = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not path.is_file() or not path.suffix:
        return None
    token = _register(path)
    stem = path.stem
    extension = path.suffix[1:]
    return (
        f'{stem} <tg-button type="callback_data" style="link" '
        f'data="{FILE_CALLBACK_PREFIX}{token}">{extension}</tg-button>{trailing}'
    )


def _replace_outside_fences(segment: str) -> str:
    def replace_link(match: re.Match[str]) -> str:
        replacement = _button_for_path(match.group(1))
        return replacement if replacement is not None else match.group(0)

    segment = _MARKDOWN_LOCAL_LINK_RE.sub(replace_link, segment)

    def replace_inline(match: re.Match[str]) -> str:
        replacement = _button_for_path(match.group(1))
        return replacement if replacement is not None else match.group(0)

    segment = _INLINE_CODE_RE.sub(replace_inline, segment)

    def replace_bare(match: re.Match[str]) -> str:
        replacement = _button_for_path(match.group(1))
        return replacement if replacement is not None else match.group(0)

    return _BARE_ABSOLUTE_PATH_RE.sub(replace_bare, segment)


def add_file_buttons(text: str) -> str:
    """Hide existing absolute file paths behind stem + extension buttons."""
    out: list[str] = []
    last = 0
    for match in _FENCED_CODE_RE.finditer(text):
        out.append(_replace_outside_fences(text[last : match.start()]))
        out.append(match.group(0))
        last = match.end()
    out.append(_replace_outside_fences(text[last:]))
    return "".join(out)
