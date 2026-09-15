"""Rich-message file buttons backed by short callback tokens."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from pathlib import Path

from .handlers.callback_data import CB_FILE_SEND
from .config import config
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

FILE_CALLBACK_PREFIX = CB_FILE_SEND
_REGISTRY_LIMIT = 2000
_file_paths: dict[str, Path] = {}
_registry_loaded = False
_REGISTRY_FILE = config.config_dir / "file_buttons.json"

_FENCED_CODE_RE = re.compile(r"```[\s\S]*?(?:```|$)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MARKDOWN_LOCAL_LINK_RE = re.compile(r"\[[^\]\n]*\]\(<?((?:~)?/[^)>\n]+)>?\)")
_BARE_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w`])(/[^\s`<>\[\]{}()]+)")
_BARE_HOME_PATH_RE = re.compile(r"(?<![\w/`])(~\/[^\s`<>\[\]{}()]+)")
_RELATIVE_INBOX_PATH_RE = re.compile(
    r"(?<![\w/`])((?:\./)?\.ccbot-inbox/[^\s`<>\[\]{}()]+)"
)
_TRAILING_PROSE = ".,;:!?"


def _token_for_path(path: Path) -> str:
    token = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:20]
    return token


def _load_registry() -> None:
    global _registry_loaded
    if _registry_loaded:
        return
    _registry_loaded = True
    try:
        raw = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    for token, raw_path in list(raw.items())[-_REGISTRY_LIMIT:]:
        if not isinstance(token, str) or not isinstance(raw_path, str):
            continue
        path = Path(raw_path)
        if path.is_absolute() and token == _token_for_path(path):
            _file_paths[token] = path


def _save_registry() -> None:
    atomic_write_json(
        _REGISTRY_FILE,
        {token: str(path) for token, path in _file_paths.items()},
        indent=None,
    )


def _register(path: Path) -> str:
    _load_registry()
    token = _token_for_path(path)
    if _file_paths.get(token) == path:
        return token
    if token not in _file_paths and len(_file_paths) >= _REGISTRY_LIMIT:
        for old_token in list(_file_paths)[: max(1, _REGISTRY_LIMIT // 10)]:
            _file_paths.pop(old_token, None)
    _file_paths[token] = path
    try:
        _save_registry()
    except OSError as exc:
        logger.warning("file button registry save failed: %s", exc)
    return token


def resolve_file_button(token: str) -> Path | None:
    """Resolve a button token and revalidate that it still names a file."""
    _load_registry()
    path = _file_paths.get(token)
    if path is None or not path.is_file():
        return None
    return path


def _button_for_path(raw_path: str, file_base_dir: Path | None = None) -> str | None:
    trailing = ""
    candidate = raw_path
    while candidate and candidate[-1] in _TRAILING_PROSE:
        trailing = candidate[-1] + trailing
        candidate = candidate[:-1]
    candidate_path = Path(candidate).expanduser()
    try:
        if candidate_path.is_absolute():
            path = candidate_path.resolve(strict=True)
        elif file_base_dir is not None and candidate_path.parts[:1] == (
            ".ccbot-inbox",
        ):
            inbox_root = (file_base_dir / ".ccbot-inbox").resolve(strict=True)
            path = (file_base_dir / candidate_path).resolve(strict=True)
            path.relative_to(inbox_root)
        else:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    if not path.is_file() or not path.suffix:
        return None
    token = _register(path)
    stem = html.escape(path.stem)
    extension = html.escape(path.suffix.removeprefix("."))
    return (
        f'{stem} <tg-button type="callback_data" '
        f'data="{FILE_CALLBACK_PREFIX}{token}">{extension}</tg-button>{trailing}'
    )


def _replace_outside_fences(segment: str, file_base_dir: Path | None) -> str:
    def replace_link(match: re.Match[str]) -> str:
        replacement = _button_for_path(match.group(1), file_base_dir)
        return replacement if replacement is not None else match.group(0)

    segment = _MARKDOWN_LOCAL_LINK_RE.sub(replace_link, segment)

    def replace_inline(match: re.Match[str]) -> str:
        replacement = _button_for_path(match.group(1), file_base_dir)
        return replacement if replacement is not None else match.group(0)

    segment = _INLINE_CODE_RE.sub(replace_inline, segment)

    def replace_relative_inbox(match: re.Match[str]) -> str:
        replacement = _button_for_path(match.group(1), file_base_dir)
        return replacement if replacement is not None else match.group(0)

    segment = _RELATIVE_INBOX_PATH_RE.sub(replace_relative_inbox, segment)

    def replace_home_path(match: re.Match[str]) -> str:
        replacement = _button_for_path(match.group(1), file_base_dir)
        return replacement if replacement is not None else match.group(0)

    segment = _BARE_HOME_PATH_RE.sub(replace_home_path, segment)

    def replace_bare(match: re.Match[str]) -> str:
        replacement = _button_for_path(match.group(1), file_base_dir)
        return replacement if replacement is not None else match.group(0)

    return _BARE_ABSOLUTE_PATH_RE.sub(replace_bare, segment)


def add_file_buttons(text: str, file_base_dir: Path | None = None) -> str:
    """Replace local paths with a filename stem and extension download button."""
    out: list[str] = []
    last = 0
    for match in _FENCED_CODE_RE.finditer(text):
        out.append(_replace_outside_fences(text[last : match.start()], file_base_dir))
        out.append(match.group(0))
        last = match.end()
    out.append(_replace_outside_fences(text[last:], file_base_dir))
    return "".join(out)
