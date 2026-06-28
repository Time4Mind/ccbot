"""Directory browser and window picker UI for session creation.

Provides UIs in Telegram for:
  - Window picker: list[Any] unbound tmux windows for quick binding
  - Directory browser: navigate directory hierarchies to create new sessions

Key components:
  - DIRS_PER_PAGE: Number of directories shown per page
  - User state keys for tracking browse/picker session
  - build_directory_browser: Build directory browser UI
  - clear_window_picker_state: Clear picker state from user_data
  - clear_browse_state: Clear browsing state from user_data
"""

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..session import ClaudeSession

from ..config import config
from ..i18n import t
from .callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_SESSION_BACK,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_PAGE,
    CB_SESSION_SELECT,
)

# Directories per page in directory browser
DIRS_PER_PAGE = 6

# Sessions per page in the session picker (after the dir is chosen).
SESSIONS_PER_PAGE = 8

# User state keys
STATE_KEY = "state"
STATE_BROWSING_DIRECTORY = "browsing_directory"
STATE_SELECTING_WINDOW = "selecting_window"
BROWSE_PATH_KEY = "browse_path"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"  # Cache of subdirs for current path
UNBOUND_WINDOWS_KEY = "unbound_windows"  # Cache of (name, cwd) tuples
STATE_SELECTING_SESSION = "selecting_session"
SESSIONS_KEY = "cached_sessions"  # Cache of ClaudeSession list[Any]
SESSIONS_PAGE_KEY = "sessions_page"  # current page index in session picker

logger = logging.getLogger(__name__)

# Pending-text stash: a message the user typed while no active session
# existed is held in user_data until a session is created, then forwarded.
PENDING_TEXT_KEY = "_pending_text"

# Max age before a stashed pending message is treated as abandoned.
# context.user_data lives in memory and is NOT cleared between messages —
# only a bot restart wipes it. Without an expiry a stash can survive for
# hours and then be injected into an unrelated session created much later
# (the 2026-06-28 "medical insurance" misroute: a 01:05 message resurfaced
# in a 10:34 session). 10 min is generous for a browser/picker flow yet
# kills any multi-hour leak.
PENDING_TEXT_TTL_S = 600.0


def stash_pending_text(user_data: dict[str, Any] | None, text: str) -> None:
    """Hold the user's text while a directory/session picker is up.

    Stamped with a timestamp so ``take_pending_text`` can reject a stale
    stash. See ``PENDING_TEXT_TTL_S``.
    """
    if user_data is not None:
        user_data[PENDING_TEXT_KEY] = {"text": text, "ts": time.time()}


def take_pending_text(
    user_data: dict[str, Any] | None, max_age_s: float | None = PENDING_TEXT_TTL_S
) -> str | None:
    """Pop the stashed pending text, returning it only if still fresh.

    Always clears the slot (a leaked stash must never re-fire). Returns
    None when absent, empty, or older than ``max_age_s``. Tolerates the
    legacy bare-string format (treated as fresh) for state in flight
    across a deploy.
    """
    if not user_data:
        return None
    raw = user_data.pop(PENDING_TEXT_KEY, None)
    if raw is None:
        return None
    if isinstance(raw, str):  # legacy pre-TTL format
        return raw or None
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    if not text:
        return None
    age = time.time() - float(raw.get("ts", 0.0))
    if max_age_s is not None and age > max_age_s:
        logger.info(
            "Dropping stale pending text (age=%.0fs > %.0fs, len=%d)",
            age,
            max_age_s,
            len(text),
        )
        return None
    return text


def clear_browse_state(user_data: dict[str, Any] | None) -> None:
    """Clear directory browsing state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(BROWSE_PATH_KEY, None)
        user_data.pop(BROWSE_PAGE_KEY, None)
        user_data.pop(BROWSE_DIRS_KEY, None)


def clear_window_picker_state(user_data: dict[str, Any] | None) -> None:
    """Clear window picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(UNBOUND_WINDOWS_KEY, None)


def clear_session_picker_state(user_data: dict[str, Any] | None) -> None:
    """Clear session picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(SESSIONS_KEY, None)


def build_directory_browser(
    current_path: str, page: int = 0, *, user_id: int
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI.

    Returns: (text, keyboard, subdirs) where subdirs is the full list[Any] for caching.
    """
    path = Path(current_path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        path = Path.home()

    try:
        # Sort by mtime descending — most recently changed directories first.
        # Fall back to alphabetical for any directory whose stat fails.
        candidates: list[tuple[float, str]] = []
        for d in path.iterdir():
            if not d.is_dir():
                continue
            if not config.show_hidden_dirs and d.name.startswith("."):
                continue
            try:
                m = d.stat().st_mtime
            except OSError:
                m = 0.0
            candidates.append((m, d.name))
        candidates.sort(key=lambda t: (-t[0], t[1].lower()))
        subdirs = [name for _, name in candidates]
    except (PermissionError, OSError):
        subdirs = []

    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start : start + DIRS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_dirs), 2):
        row = []
        for j, name in enumerate(page_dirs[i : i + 2]):
            display = name[:12] + "…" if len(name) > 13 else name
            # Use global index (start + i + j) to avoid long dir names in callback_data
            idx = start + i + j
            row.append(
                InlineKeyboardButton(
                    f"📁 {display}", callback_data=f"{CB_DIR_SELECT}{idx}"
                )
            )
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_DIR_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_DIR_PAGE}{page + 1}")
            )
        buttons.append(nav)

    action_row: list[InlineKeyboardButton] = []
    # Allow going up unless at filesystem root
    if path != path.parent:
        action_row.append(
            InlineKeyboardButton(t(user_id, "dir.btn.up"), callback_data=CB_DIR_UP)
        )
    action_row.append(
        InlineKeyboardButton(t(user_id, "dir.btn.select"), callback_data=CB_DIR_CONFIRM)
    )
    action_row.append(
        InlineKeyboardButton(t(user_id, "btn.menu"), callback_data=CB_DIR_CANCEL)
    )
    buttons.append(action_row)

    display_path = str(path).replace(str(Path.home()), "~")
    title = t(user_id, "dir.title")
    current = t(user_id, "dir.current", path=display_path)
    tail = t(user_id, "dir.empty") if not subdirs else t(user_id, "dir.hint")
    text = f"{title}\n\n{current}\n\n{tail}"

    return text, InlineKeyboardMarkup(buttons), subdirs


def _relative_time(file_path: str) -> str:
    """Format file mtime as a human-readable relative time string."""
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return ""
    delta = int(time.time() - mtime)
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


def build_session_picker(
    sessions: list[ClaudeSession],
    *,
    page: int = 0,
    summary_resolver: Callable[[ClaudeSession], str] | None = None,
    user_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build session picker UI for resuming an existing Claude session.

    Args:
        sessions: All ClaudeSession objects in the chosen dir (sorted by recency).
        page: Page index in the paginated picker (0-based).
        summary_resolver: Optional callable that returns a human-readable label
            for a session. When None, falls back to the JSONL `summary` field.

    Returns: (text, keyboard).
    """
    total = len(sessions)
    pages = max(1, (total + SESSIONS_PER_PAGE - 1) // SESSIONS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * SESSIONS_PER_PAGE
    chunk = sessions[start : start + SESSIONS_PER_PAGE]

    def _label_for(s: ClaudeSession) -> str:
        text = summary_resolver(s) if summary_resolver else s.summary
        return text or "untitled"

    lines = [
        t(user_id, "picker.title"),
        "",
        t(user_id, "picker.summary", page=page + 1, pages=pages, total=total),
        "",
    ]
    for n, s in enumerate(chunk, start=1):
        summary = _label_for(s)
        if len(summary) > 60:
            summary = summary[:59] + "…"
        rel = _relative_time(s.file_path)
        time_str = f" ({rel})" if rel else ""
        if s.token_total >= 1000:
            tok = f"{s.token_total // 1000}k"
        else:
            tok = f"{s.token_total}"
        lines.append(f"{n}. {summary}\n   {s.message_count} msgs / {tok}{time_str}")

    buttons: list[list[InlineKeyboardButton]] = []
    # One row of numeric pickers per page (max 8 per page → up to 4+4).
    num_row: list[InlineKeyboardButton] = []
    for n, _s in enumerate(chunk, start=1):
        num_row.append(
            InlineKeyboardButton(
                str(n),
                # Callback data carries the absolute index, not the local one.
                callback_data=f"{CB_SESSION_SELECT}{start + (n - 1)}",
            )
        )
        if len(num_row) == 4:
            buttons.append(num_row)
            num_row = []
    if num_row:
        buttons.append(num_row)

    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_SESSION_PAGE}{page - 1}")
            )
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_SESSION_PAGE}{page + 1}")
            )
        buttons.append(nav)

    buttons.append(
        [
            InlineKeyboardButton(
                t(user_id, "picker.btn.start_fresh"), callback_data=CB_SESSION_NEW
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                t(user_id, "picker.btn.back_to_dirs"), callback_data=CB_SESSION_BACK
            ),
            InlineKeyboardButton(
                t(user_id, "btn.menu"), callback_data=CB_SESSION_CANCEL
            ),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons)
