"""Compatibility facade for inbound Telegram message handlers.

The implementation is split by responsibility across ``_messages_shared``,
``_messages_media``, ``_messages_voice`` and ``_messages_text``.  This module
intentionally remains the canonical import and monkeypatch surface: before a
delegated function runs, its implementation module receives the current
attributes from this facade.  Existing tests and integrations that replace
``ccbot.bot.messages.session_manager`` or a private helper therefore retain
the same lookup semantics they had when all functions lived in this file.
"""

from __future__ import annotations

import inspect
import logging
from functools import wraps
from types import ModuleType
from typing import Any, Callable, cast

from . import _messages_media, _messages_shared, _messages_text, _messages_voice
from ._session_create import create_and_activate_session

_IMPLEMENTATION_MODULES: tuple[ModuleType, ...] = (
    _messages_shared,
    _messages_media,
    _messages_voice,
    _messages_text,
)


def _publish_implementation_names(module: ModuleType) -> None:
    """Expose constants, dependencies, state objects and classes unchanged."""
    for name, value in vars(module).items():
        if not name.startswith("__"):
            globals()[name] = value


for _module in _IMPLEMENTATION_MODULES:
    _publish_implementation_names(_module)

# Keep the historical logger category even though implementations now live in
# private sibling modules.  It is synchronized into every implementation at
# each facade call.
logger = logging.getLogger(__name__)

_FUNCTION_OWNERS: dict[str, ModuleType] = {
    # Shared ordering, delivery, live-card bracket and pending-UI handling.
    "_voice_transcript_checkpoint": _messages_shared,
    "_transcript_contains_voice_text": _messages_shared,
    "_wait_for_voice_transcript": _messages_shared,
    "_send_with_delivery_proof": _messages_shared,
    "_enqueue_voice": _messages_shared,
    "_wait_for_voice": _messages_shared,
    "_await_prior_voice": _messages_shared,
    "_release_voice": _messages_shared,
    "_append_dropped_queue_notice": _messages_shared,
    "_download_voice_bytes": _messages_shared,
    "_is_file_too_big": _messages_shared,
    "_card_repost_bracket": _messages_shared,
    "_pane_has_interactive_ui": _messages_shared,
    "_intercept_if_pending_ui": _messages_shared,
    "forward_command_handler": _messages_shared,
    # Forwarded content and Telegram file intake.
    "_forward_attribution": _messages_media,
    "_hidden_link_urls": _messages_media,
    "unsupported_content_handler": _messages_media,
    "_forward_inbox_file": _messages_media,
    "photo_handler": _messages_media,
    "document_handler": _messages_media,
    # Voice intake and transcription.
    "_clear_voice_pending_marker": _messages_voice,
    "voice_handler": _messages_voice,
    "_process_voice": _messages_voice,
    # Text routing and bash-output capture.
    "cancel_bash_capture": _messages_text,
    "_capture_bash_output": _messages_text,
    "_route_reply_quote": _messages_text,
    "_resolve_active_window": _messages_text,
    "_maybe_start_bash_capture": _messages_text,
    "_dispatch_text_to_active": _messages_text,
    "text_handler": _messages_text,
}

# Save function objects before synchronization replaces implementation-module
# globals with facade proxies.  Proxies always call these stable originals.
_ORIGINAL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    name: getattr(owner, name) for name, owner in _FUNCTION_OWNERS.items()
}

_FACADE_INTERNALS = {
    "_IMPLEMENTATION_MODULES",
    "_FUNCTION_OWNERS",
    "_ORIGINAL_FUNCTIONS",
    "_FACADE_INTERNALS",
    "_publish_implementation_names",
    "_sync_implementation_names",
    "_make_proxy",
    "_module",
}


def _sync_implementation_names() -> None:
    """Push the facade's current (possibly monkeypatched) names downstream."""
    facade_names = {
        name: value
        for name, value in globals().items()
        if not name.startswith("__") and name not in _FACADE_INTERNALS
    }
    for module in _IMPLEMENTATION_MODULES:
        vars(module).update(facade_names)


def _make_proxy(name: str) -> Callable[..., Any]:
    original = _ORIGINAL_FUNCTIONS[name]
    if inspect.iscoroutinefunction(original):

        @wraps(original)
        async def async_proxy(*args: Any, **kwargs: Any) -> Any:
            _sync_implementation_names()
            return await original(*args, **kwargs)

        return async_proxy

    @wraps(original)
    def sync_proxy(*args: Any, **kwargs: Any) -> Any:
        _sync_implementation_names()
        return original(*args, **kwargs)

    return sync_proxy


for _name in _FUNCTION_OWNERS:
    globals()[_name] = _make_proxy(_name)

# Explicit aliases make the facade's stable handler surface visible to static
# tooling; each value is the proxy installed above, not the implementation
# function itself.
forward_command_handler = cast(Callable[..., Any], globals()["forward_command_handler"])
unsupported_content_handler = cast(
    Callable[..., Any], globals()["unsupported_content_handler"]
)
photo_handler = cast(Callable[..., Any], globals()["photo_handler"])
document_handler = cast(Callable[..., Any], globals()["document_handler"])
voice_handler = cast(Callable[..., Any], globals()["voice_handler"])
text_handler = cast(Callable[..., Any], globals()["text_handler"])

# Preserve the deliberately narrow historical star-import contract.  Named
# imports of all handlers/private helpers above continue to work as before.
__all__ = ["create_and_activate_session"]
