"""kb-mode keyboard + pane-capture helpers for the live card.

Stateless building blocks behind ``handlers.notifications`` kb-mode
(Task #41) and the inline-screenshot card path (Task #48):

* ``build_kb_mode_keyboard`` — the 3×3 navigation grid shown when the
  card msg is flipped into kb-mode (an interactive prompt is waiting).
* ``_capture_pane_png`` — render the tmux pane to PNG bytes + a content
  hash so callers can skip pointless ``editMessageMedia`` calls.

Neither touches the module-global card registries; the stateful
``enter_kb_mode`` / ``exit_kb_mode`` / ``has_pending_kb`` orchestration
stays in ``handlers.notifications`` which re-exports both names here.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..i18n import t

logger = logging.getLogger(__name__)

# ``_capture_pane_png`` is underscore-private but re-exported by the
# ``handlers.notifications`` facade. Listing it in ``__all__`` marks it
# as part of this module's intended interface so pyright's strict
# ``reportPrivateUsage`` doesn't flag the re-export.
__all__ = ["_capture_pane_png", "build_kb_mode_keyboard"]


async def _capture_pane_png(window_id: str) -> tuple[bytes | None, str]:
    """Render the tmux pane to PNG bytes and return its content hash.

    Returns (png_bytes, content_hash). On capture failure: (None, "").
    The hash lets callers skip pointless ``editMessageMedia`` calls when
    the pane hasn't changed since last refresh.
    """
    import hashlib

    from ..screenshot import text_to_image
    from ..tmux_manager import tmux_manager

    if not window_id:
        return None, ""
    w = await tmux_manager.find_window_by_id(window_id)
    if w is None:
        return None, ""
    try:
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
    except Exception as e:
        logger.debug("capture_pane_png: capture failed: %s", e)
        return None, ""
    if not text:
        return None, ""
    pane_hash = hashlib.md5(text.encode("utf-8", "replace")).hexdigest()
    try:
        png = await text_to_image(text, with_ansi=True)
    except Exception as e:
        logger.debug("capture_pane_png: render failed: %s", e)
        return None, ""
    return png, pane_hash


def build_kb_mode_keyboard(
    user_id: int, window_id: str, ui_name: str = ""
) -> InlineKeyboardMarkup:
    """Build the kb-mode keyboard shown when the card msg is in kb-mode.

    Layout (per current /screenshot kb-mode 3×3 grid):
        [␣ Space] [↑] [⇥ Tab]
        [←]       [↓] [→]
        [⎋ Esc]   [^C] [⏎ Enter]
        [🔙 Back] [+ new] [≡ Menu]

    Tapping arrow / Space / Tab / Esc / Enter / ^C dispatches via CB_ASK_*
    (existing keystroke handlers in handlers/interactive_ui.py).
    """
    from .callback_data import (
        CB_ASK_DOWN,
        CB_ASK_ENTER,
        CB_ASK_ESC,
        CB_ASK_LEFT,
        CB_ASK_RIGHT,
        CB_ASK_SPACE,
        CB_ASK_TAB,
        CB_ASK_UP,
        CB_FT_MORE,
        CB_KB_BACK,
        CB_SW_NEW,
    )

    def kb(label: str, prefix: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=f"{prefix}{window_id}"[:64])

    rows: list[list[InlineKeyboardButton]] = []
    vertical_only = ui_name == "RestoreCheckpoint"
    rows.append(
        [kb("␣ Space", CB_ASK_SPACE), kb("↑", CB_ASK_UP), kb("⇥ Tab", CB_ASK_TAB)]
    )
    if vertical_only:
        rows.append([kb("↓", CB_ASK_DOWN)])
    else:
        rows.append([kb("←", CB_ASK_LEFT), kb("↓", CB_ASK_DOWN), kb("→", CB_ASK_RIGHT)])
    rows.append(
        [kb("⎋ Esc", CB_ASK_ESC), kb("^C", "aq:cc:"), kb("⏎ Enter", CB_ASK_ENTER)]
    )
    rows.append(
        [
            InlineKeyboardButton("🔙 Back", callback_data=CB_KB_BACK),
            InlineKeyboardButton("+ new", callback_data=CB_SW_NEW),
            InlineKeyboardButton(t(user_id, "btn.menu"), callback_data=CB_FT_MORE),
        ]
    )
    return InlineKeyboardMarkup(rows)
