"""Backend activation/install callbacks extracted from the settings router."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import CallbackQuery
from telegram.ext import ContextTypes

from ... import agent_install
from ...claude_auth import credentials_state
from ...default_session import ensure_default_session
from ...handlers.menu import build_footer_keyboard, render_settings_group_text
from ...handlers.message_sender import safe_edit, safe_send
from ...i18n import t
from ...session import session_manager

from ...config import config

logger = logging.getLogger(__name__)

_install_inflight: set[tuple[int, str]] = set()


async def _ensure_auth(bot: Any, user_id: int, backend: str) -> None:
    from ..commands.auth import ensure_codex_authenticated, start_login

    if backend == "codex":
        await ensure_codex_authenticated(bot, user_id, backend="codex")
    elif backend == "zcode":
        if await ensure_codex_authenticated(bot, user_id, backend="zcode"):
            from ...node_worker import TmuxWorkerExecutor
            from ...node_backend_readiness import (
                health_backend_fields,
                update_backend_health,
            )

            probe = TmuxWorkerExecutor(workdir=".", zcode_command=config.zcode_command)
            configured = session_manager.get_enabled_backends(user_id)
            ready = await probe.ready_backends(configured, max_age=0)
            node = session_manager.get_node("local")
            if node is not None:
                update_backend_health(node, health_backend_fields(configured, ready))
                session_manager.save_state()
    elif not credentials_state().present:
        await start_login(bot, user_id, backend="claude")


async def _install_and_activate(
    query: CallbackQuery, user_id: int, backend: str
) -> None:
    key = (user_id, backend)
    if key in _install_inflight:
        return
    _install_inflight.add(key)
    bot = query.get_bot()

    async def progress(message: str) -> None:
        await safe_send(bot, user_id, message)

    try:
        if not await agent_install.install(backend, progress):
            return
        session_manager.set_backend_enabled(user_id, backend, True)
        await safe_send(bot, user_id, f"✅ {backend.capitalize()} enabled.")
        await _ensure_auth(bot, user_id, backend)
    except Exception as exc:
        logger.exception("Agent install/activation failed backend=%s: %s", backend, exc)
        try:
            await safe_send(bot, user_id, f"❌ {backend} activation failed: `{exc}`")
        except Exception:
            pass
    finally:
        _install_inflight.discard(key)


async def handle_agent_value(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
    value: str,
) -> bool:
    """Apply an Agent-screen value; True means response already completed."""
    if value.startswith("toggle:"):
        backend = value.removeprefix("toggle:")
        if backend not in ("claude", "codex", "zcode"):
            await query.answer("Unknown backend", show_alert=True)
            return True
        enabled = backend not in session_manager.get_enabled_backends(user.id)
        if enabled and not agent_install.is_available(backend):
            asyncio.create_task(
                _install_and_activate(query, user.id, backend),
                name=f"install-agent:{backend}:{user.id}",
            )
            await query.answer("Installing…")
            await safe_edit(
                query,
                render_settings_group_text(user.id, "settings_agent"),
                reply_markup=build_footer_keyboard(user.id, screen="settings_agent"),
            )
            return True
        try:
            session_manager.set_backend_enabled(user.id, backend, enabled)
        except RuntimeError:
            await query.answer(t(user.id, "toast.last_backend"), show_alert=True)
            return True
        asyncio.create_task(ensure_default_session(context.bot, user.id))
        if enabled:
            asyncio.create_task(
                _ensure_auth(context.bot, user.id, backend),
                name=f"auth-agent:{backend}:{user.id}",
            )
        return False
    if value.startswith("default:"):
        backend = value.removeprefix("default:")
        try:
            session_manager.set_default_backend(user.id, backend)
        except ValueError:
            await query.answer("Backend is disabled", show_alert=True)
            return True
        asyncio.create_task(ensure_default_session(context.bot, user.id))
    return False
