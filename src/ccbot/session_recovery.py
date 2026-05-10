"""Startup hygiene for SessionManager — separated to keep session.py small.

These helpers run from app.py's `post_init` (and from `load_session_map`)
to handle three startup-only concerns:

  1. ``reconcile_with_tmux`` — Sessions whose tmux window vanished get
     state=lost; corresponding ``active_sessions`` entries are dropped.
  2. ``resolve_stale_window_ids`` — tmux server restart resets window
     IDs; we re-map persisted state by display-name where possible,
     drop entries with no live counterpart. Also handles legacy
     window_name keys from the old supergroup-routing format.
  3. ``cleanup_session_map_*`` — drop session_map.json entries for
     windows that no longer exist (closed externally) and entries with
     legacy keys.

The functions take a ``SessionManager`` argument and mutate it directly
— that's the cheapest API given how tangled the state is, and these are
private to the module/SessionManager pair.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import aiofiles

from .config import config
from .session_models import WindowState
from .tmux_manager import tmux_manager
from .utils import atomic_write_json

if TYPE_CHECKING:
    from .session import SessionManager

logger = logging.getLogger(__name__)


async def reconcile_with_tmux(mgr: "SessionManager") -> int:
    """Mark sessions whose tmux window vanished as ``lost``; clear stale actives."""
    windows = await tmux_manager.list_windows()
    live_ids = {w.window_id for w in windows}
    lost = 0
    for sess in mgr.sessions.values():
        if sess.state in ("active", "idle") and sess.window_id:
            if sess.window_id not in live_ids:
                sess.state = "lost"
                lost += 1
    if lost:
        mgr.save_state()
        logger.info("Reconcile: marked %d sessions as lost on startup", lost)
    for uid, sid in list(mgr.active_sessions.items()):
        sess = mgr.sessions.get(sid)
        if sess is None or sess.state == "lost":
            del mgr.active_sessions[uid]
    mgr.save_state()
    return lost


async def resolve_stale_window_ids(mgr: "SessionManager") -> None:
    """Re-resolve persisted window IDs against live tmux windows.

    Two cases handled in one pass:

    - Old-format migration: window_name keys → window_id keys.
    - Stale IDs: window_id missing in tmux but display name matches a
      live window — re-map; otherwise drop.

    Also runs the session_map.json cleanup helpers below.
    """
    windows = await tmux_manager.list_windows()
    live_by_name: dict[str, str] = {}
    live_ids: set[str] = set()
    for w in windows:
        live_by_name[w.window_name] = w.window_id
        live_ids.add(w.window_id)

    changed = False

    # --- Migrate window_states ---
    new_window_states: dict[str, WindowState] = {}
    for key, ws in mgr.window_states.items():
        if mgr.is_window_id(key):
            if key in live_ids:
                new_window_states[key] = ws
            else:
                display = mgr.window_display_names.get(key, ws.window_name or key)
                new_id = live_by_name.get(display)
                if new_id:
                    logger.info(
                        "Re-resolved stale window_id %s -> %s (name=%s)",
                        key,
                        new_id,
                        display,
                    )
                    new_window_states[new_id] = ws
                    ws.window_name = display
                    mgr.window_display_names[new_id] = display
                    mgr.window_display_names.pop(key, None)
                    changed = True
                else:
                    logger.info(
                        "Dropping stale window_state: %s (name=%s)", key, display
                    )
                    changed = True
        else:
            # Old format: key is window_name
            new_id = live_by_name.get(key)
            if new_id:
                logger.info("Migrating window_state key %s -> %s", key, new_id)
                ws.window_name = key
                new_window_states[new_id] = ws
                mgr.window_display_names[new_id] = key
                changed = True
            else:
                logger.info(
                    "Dropping old-format window_state: %s (no live window)", key
                )
                changed = True
    mgr.window_states = new_window_states

    # --- Migrate user_window_offsets ---
    for uid, offsets in mgr.user_window_offsets.items():
        new_offsets: dict[str, int] = {}
        for key, offset in offsets.items():
            if mgr.is_window_id(key):
                if key in live_ids:
                    new_offsets[key] = offset
                else:
                    display = mgr.window_display_names.get(key, key)
                    new_id = live_by_name.get(display)
                    if new_id:
                        new_offsets[new_id] = offset
                        changed = True
                    else:
                        changed = True
            else:
                new_id = live_by_name.get(key)
                if new_id:
                    new_offsets[new_id] = offset
                    changed = True
                else:
                    changed = True
        mgr.user_window_offsets[uid] = new_offsets

    if changed:
        mgr.save_state()
        logger.info("Startup re-resolution complete")

    await cleanup_stale_session_map_entries(mgr, live_ids)
    await cleanup_old_format_session_map_keys(mgr)


async def cleanup_old_format_session_map_keys(mgr: "SessionManager") -> None:
    """Drop session_map.json entries keyed by window_name (pre-window_id format)."""
    if not config.session_map_file.exists():
        return
    try:
        async with aiofiles.open(config.session_map_file, "r") as f:
            content = await f.read()
        session_map = json.loads(content)
    except (json.JSONDecodeError, OSError):
        return

    prefix = f"{config.tmux_session_name}:"
    old_keys = [
        key
        for key in session_map
        if key.startswith(prefix) and not mgr.is_window_id(key[len(prefix) :])
    ]
    if not old_keys:
        return

    for key in old_keys:
        del session_map[key]
    atomic_write_json(config.session_map_file, session_map)
    logger.info(
        "Cleaned up %d old-format session_map keys: %s", len(old_keys), old_keys
    )


async def cleanup_stale_session_map_entries(
    mgr: "SessionManager", live_ids: set[str]
) -> None:
    """Drop session_map.json entries for tmux windows that no longer exist."""
    if not config.session_map_file.exists():
        return
    try:
        async with aiofiles.open(config.session_map_file, "r") as f:
            content = await f.read()
        session_map = json.loads(content)
    except (json.JSONDecodeError, OSError):
        return

    prefix = f"{config.tmux_session_name}:"
    stale_keys = [
        key
        for key in session_map
        if key.startswith(prefix)
        and mgr.is_window_id(key[len(prefix) :])
        and key[len(prefix) :] not in live_ids
    ]
    if not stale_keys:
        return

    for key in stale_keys:
        del session_map[key]
        logger.info("Removed stale session_map entry: %s", key)

    atomic_write_json(config.session_map_file, session_map)
    logger.info(
        "Cleaned up %d stale session_map entries (windows no longer in tmux)",
        len(stale_keys),
    )
