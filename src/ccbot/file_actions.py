"""Rich-message file buttons backed by short callback tokens."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeAlias
from urllib.parse import unquote

from .handlers.callback_data import CB_FILE_SEND
from .config import config
from .session_models import Session
from .transfer_runtime import get_node_runtime
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

FILE_CALLBACK_PREFIX = CB_FILE_SEND
_REGISTRY_LIMIT = 2000


@dataclass(frozen=True)
class RemoteFileReference:
    node_id: str
    session_id: str
    path: str
    name: str
    size: int
    version: str = ""


@dataclass(frozen=True)
class RemoteFileButtonContext:
    references: dict[str, RemoteFileReference]


FileReference: TypeAlias = Path | RemoteFileReference
FileButtonContext: TypeAlias = Path | RemoteFileButtonContext | None


_file_paths: dict[str, FileReference] = {}
_registry_loaded = False
_REGISTRY_FILE = config.config_dir / "file_buttons.json"
_remote_validation_cache: dict[
    tuple[str, str, str], tuple[float, RemoteFileReference | None]
] = {}
_REMOTE_VALID_SECONDS = 300.0
_REMOTE_REJECTED_SECONDS = 15.0

# Treat a whole link as one token, including web links and missing files.
# Rescanning pieces of a rejected link could turn a suffix into an absolute path.
_FILE_MENTION = re.compile(
    r"```[\s\S]*?(?:```|$)"
    r"|\[[^\]\n]*\]\((?:<(?P<angle>[^>\n]+)>|(?P<link>[^)\n]+))\)"
    r"|`(?P<inline>[^`\n]+)`"
    r"|https?://[^\s`<>]+"
    r"|(?<![\w/`])(?P<bare>(?:~?/|(?:\./)?\.ccbot-inbox/)[^\s`<>\[\]{}()]+)"
)
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_TRAILING_PROSE = ".,;:!?"


def _token_for_reference(reference: FileReference) -> str:
    if isinstance(reference, Path):
        # Preserve tokens already persisted by the local-only implementation.
        identity = str(reference)
    else:
        identity = (
            f"remote\0{reference.node_id}\0{reference.session_id}\0{reference.path}"
        )
        if reference.version:
            identity += f"\0{reference.version}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


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
    for token, raw_reference in list(raw.items())[-_REGISTRY_LIMIT:]:
        if not isinstance(token, str):
            continue
        reference: FileReference | None = None
        if isinstance(raw_reference, str):
            path = Path(raw_reference)
            if path.is_absolute():
                reference = path
        elif isinstance(raw_reference, dict) and raw_reference.get("kind") == "remote":
            try:
                reference = RemoteFileReference(
                    node_id=str(raw_reference["node_id"]),
                    session_id=str(raw_reference["session_id"]),
                    path=str(raw_reference["path"]),
                    name=str(raw_reference["name"]),
                    size=int(raw_reference["size"]),
                    version=str(raw_reference.get("version", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
        if reference is not None and token == _token_for_reference(reference):
            _file_paths[token] = reference


def _save_registry() -> None:
    serialized: dict[str, object] = {}
    for token, reference in _file_paths.items():
        if isinstance(reference, Path):
            serialized[token] = str(reference)
        else:
            serialized[token] = {"kind": "remote", **asdict(reference)}
    atomic_write_json(
        _REGISTRY_FILE,
        serialized,
        indent=None,
    )


def _register(reference: FileReference) -> str:
    _load_registry()
    token = _token_for_reference(reference)
    if _file_paths.get(token) == reference:
        return token
    if token not in _file_paths and len(_file_paths) >= _REGISTRY_LIMIT:
        for old_token in list(_file_paths)[: max(1, _REGISTRY_LIMIT // 10)]:
            _file_paths.pop(old_token, None)
    _file_paths[token] = reference
    try:
        _save_registry()
    except OSError as exc:
        logger.warning("file button registry save failed: %s", exc)
    return token


def resolve_file_button(token: str) -> Path | None:
    """Resolve a button token and revalidate that it still names a file."""
    _load_registry()
    reference = _file_paths.get(token)
    if not isinstance(reference, Path) or not reference.is_file():
        return None
    return reference


def resolve_file_reference(token: str) -> FileReference | None:
    """Resolve a persisted local or remote button reference."""
    _load_registry()
    reference = _file_paths.get(token)
    if isinstance(reference, Path):
        return reference if reference.is_file() else None
    return reference


def _mentioned_path(match: re.Match[str]) -> str | None:
    if match.lastgroup is None:
        return None
    raw = match[match.lastgroup]
    if raw.startswith("//") or _URI_SCHEME.match(raw):
        return None
    if match.lastgroup in ("link", "angle"):
        if "?" in raw:
            return None
        path = unquote(raw.partition("#")[0])
        if not path or path.startswith("//"):
            return None
        if path.startswith(("/", "~")) and not raw.startswith(("/", "~/")):
            return None
        return path
    if raw.startswith(("/", "~/", ".ccbot-inbox/", "./.ccbot-inbox/")):
        return raw
    return None


async def file_button_context_for_session(
    session: Session, text: str
) -> FileButtonContext:
    """Validate candidate output paths on the session's owning node."""
    node_id = getattr(session, "node_id", "local")
    workdir = getattr(session, "workdir", "")
    if node_id == "local":
        return Path(workdir) if workdir else None
    routing_id = getattr(session, "worker_session_id", "") or getattr(
        session, "claude_session_id", ""
    )
    runtime = get_node_runtime(node_id)
    references: dict[str, RemoteFileReference] = {}
    if runtime is None or not routing_id:
        logger.info(
            "remote file button validation skipped node=%s session=%s reason=offline",
            node_id,
            session.id,
        )
        return RemoteFileButtonContext(references)
    now = time.monotonic()
    paths = dict.fromkeys(
        path.rstrip(_TRAILING_PROSE)
        for match in _FILE_MENTION.finditer(text)
        if (path := _mentioned_path(match))
    )
    for candidate in paths:
        cache_key = (node_id, routing_id, candidate)
        cached = _remote_validation_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            if cached[1] is not None:
                references[candidate] = cached[1]
            continue
        reference: RemoteFileReference | None = None
        reason = "unavailable"
        try:
            result = await runtime.stat_session_file(node_id, routing_id, candidate)
            if result.get("ok", False):
                reference = RemoteFileReference(
                    node_id=node_id,
                    session_id=routing_id,
                    path=str(result["path"]),
                    name=str(result["name"]),
                    size=int(result["size"]),
                    version=str(result.get("version", "")),
                )
            else:
                reason = str(result.get("error", reason))
        except Exception as exc:
            reason = str(exc).strip() or type(exc).__name__
        ttl = (
            _REMOTE_VALID_SECONDS if reference is not None else _REMOTE_REJECTED_SECONDS
        )
        _remote_validation_cache[cache_key] = (now + ttl, reference)
        if reference is None:
            logger.info(
                "remote file button candidate rejected node=%s session=%s path=%s "
                "reason=%s",
                node_id,
                session.id,
                candidate,
                reason,
            )
            continue
        references[candidate] = reference
        _register(reference)
    return RemoteFileButtonContext(references)


def _button_for_path(
    raw_path: str, file_base_dir: FileButtonContext = None
) -> str | None:
    trailing = ""
    candidate = raw_path
    while candidate and candidate[-1] in _TRAILING_PROSE:
        trailing = candidate[-1] + trailing
        candidate = candidate[:-1]
    if isinstance(file_base_dir, RemoteFileButtonContext):
        reference = file_base_dir.references.get(candidate)
        if reference is None:
            return None
        token = _register(reference)
        display_path = Path(reference.name)
        stem = html.escape(display_path.stem)
        extension = html.escape(display_path.suffix.removeprefix("."))
        return (
            f'{stem} <tg-button type="callback_data" '
            f'data="{FILE_CALLBACK_PREFIX}{token}">{extension}</tg-button>{trailing}'
        )
    try:
        candidate_path = Path(candidate).expanduser()
        if candidate_path.is_absolute():
            path = candidate_path.resolve(strict=True)
        elif file_base_dir is not None:
            root = file_base_dir.resolve(strict=True)
            path = (root / candidate_path).resolve(strict=True)
            path.relative_to(root)
            if candidate_path.parts[:1] == (".ccbot-inbox",):
                path.relative_to((root / ".ccbot-inbox").resolve(strict=True))
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


def add_file_buttons(text: str, file_base_dir: FileButtonContext = None) -> str:
    """Replace file mentions with a filename stem and extension download button."""

    def replace(match: re.Match[str]) -> str:
        path = _mentioned_path(match)
        if path is not None:
            return _button_for_path(path, file_base_dir) or match[0]
        return match[0]

    return _FILE_MENTION.sub(replace, text)
