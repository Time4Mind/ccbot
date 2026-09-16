"""kb-mode keyboard + pane-capture helpers for the live card.

Stateless building blocks behind ``handlers.notifications`` kb-mode
(Task #41) and the inline-screenshot card path (Task #48):

* ``build_kb_mode_keyboard`` — direct option buttons for native model/effort
  pickers, otherwise the 3×3 navigation grid shown for interactive prompts.
* ``_capture_pane_png`` — render the tmux pane to PNG bytes + a content
  hash so callers can skip pointless ``editMessageMedia`` calls.

Neither touches the module-global card registries; the stateful
``enter_kb_mode`` / ``exit_kb_mode`` / ``has_pending_kb`` orchestration
stays in ``handlers.notifications`` which re-exports both names here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..i18n import t

logger = logging.getLogger(__name__)

# ``_capture_pane_png`` is underscore-private but re-exported by the
# ``handlers.notifications`` facade. Listing it in ``__all__`` marks it
# as part of this module's intended interface so pyright's strict
# ``reportPrivateUsage`` doesn't flag the re-export.
__all__ = [
    "_capture_pane_png",
    "PickerMenu",
    "PickerOption",
    "build_kb_mode_keyboard",
    "parse_picker_menu",
]


@dataclass(frozen=True)
class PickerOption:
    label: str
    selected: bool = False


@dataclass(frozen=True)
class PickerMenu:
    options: tuple[PickerOption, ...]
    cursor_index: int


_PICKER_TITLE_RE = re.compile(
    r"^\s*Select\s+(?:model\b|(?:reasoning\s+)?effort\b|reasoning\s+level\b)",
    re.IGNORECASE | re.MULTILINE,
)
_NUMBERED_OPTION_RE = re.compile(r"^\s*([❯›>])?\s*(\d+)\.\s+(.+?)\s*$")
_BULLET_OPTION_RE = re.compile(r"^\s*([●○◉◯])\s+(.+?)\s*$")
_OPTION_STATE_RE = re.compile(
    r"\s*\((?:current|default|recommended)\)\s*", re.IGNORECASE
)


def _picker_label(raw: str) -> str:
    # Native pickers align the description column with 2+ spaces. Keep only
    # the selectable name, then remove state hints represented by the ✓ prefix.
    label = re.split(r"\s{2,}", raw.strip(), maxsplit=1)[0]
    return _OPTION_STATE_RE.sub(" ", label).strip()


def parse_picker_menu(prompt_content: str, ui_name: str = "") -> PickerMenu | None:
    """Parse native model and reasoning-effort selectors into Telegram options.

    Other interactive Settings pickers deliberately retain the generic
    arrow-key grid. The active value comes from an explicit ``(current)`` or
    selected bullet when available; otherwise the native cursor is the best
    representation of the currently selected model/effort.
    """
    if ui_name != "Settings" or not _PICKER_TITLE_RE.search(prompt_content):
        return None

    parsed: list[tuple[str, bool, bool]] = []
    for line in prompt_content.splitlines():
        numbered = _NUMBERED_OPTION_RE.match(line)
        if numbered:
            cursor = bool(numbered.group(1))
            raw = numbered.group(3)
            parsed.append(
                (_picker_label(raw), cursor, bool(re.search(r"\(current\)", raw, re.I)))
            )
            continue
        bullet = _BULLET_OPTION_RE.match(line)
        if bullet:
            marker = bullet.group(1)
            raw = bullet.group(2)
            parsed.append(
                (
                    _picker_label(raw),
                    marker in {"●", "◉"},
                    marker in {"●", "◉"} or bool(re.search(r"\(current\)", raw, re.I)),
                )
            )

    parsed = [item for item in parsed if item[0]]
    if not parsed:
        return None

    cursor_index = next((i for i, (_, cursor, _) in enumerate(parsed) if cursor), 0)
    selected_index = next(
        (i for i, (_, _, explicit) in enumerate(parsed) if explicit), cursor_index
    )
    return PickerMenu(
        options=tuple(
            PickerOption(label=label, selected=i == selected_index)
            for i, (label, _, _) in enumerate(parsed)
        ),
        cursor_index=cursor_index,
    )


def _utf8_complete_suffix(text: str, limit_bytes: int) -> str:
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit_bytes:
        return text
    suffix = raw[-limit_bytes:].decode("utf-8", "ignore")
    # The byte cut usually starts inside a terminal row. Prefer complete rows
    # so the screenshot begins at a meaningful visual boundary.
    if "\n" in suffix:
        suffix = suffix.split("\n", 1)[1]
    return suffix


async def _capture_pane_png(
    window_id: str, *, user_id: int | None = None
) -> tuple[bytes | None, str]:
    """Render the tmux pane to PNG bytes and return its content hash.

    Returns (png_bytes, content_hash). On capture failure: (None, "").
    The hash lets callers skip pointless ``editMessageMedia`` calls when
    the pane hasn't changed since last refresh.
    """
    import hashlib

    from ..screenshot import text_to_image
    from ..session import session_manager
    from ..tmux_manager import tmux_manager

    if not window_id:
        return None, ""
    try:
        # Reuse the status poller's persistent tmux control client.  The old
        # find-window + capture path spawned several tmux processes for every
        # rich-card refresh, even though the stable window id is already the
        # exact capture target.
        text = (await tmux_manager.capture_panes([window_id], with_ansi=True)).get(
            window_id
        )
    except Exception as e:
        logger.debug("capture_pane_png: capture failed: %s", e)
        return None, ""
    if not text:
        return None, ""
    settings = session_manager.get_user_settings(user_id) if user_id is not None else {}
    capture_kib = int(settings.get("screenshot_capture_kib", 48))
    if capture_kib not in (48, 64, 86):
        capture_kib = 48
    profile = str(settings.get("screenshot_profile", "full8"))
    if profile not in ("full8", "compact8", "fullcolor"):
        profile = "full8"

    bounded = _utf8_complete_suffix(text, capture_kib * 1024)
    max_rows, max_columns = {
        48: (120, 180),
        64: (160, 220),
        86: (220, 280),
    }[capture_kib]
    rows = bounded.splitlines()[-max_rows:]
    bounded = "\n".join(row[: max_columns * 4] for row in rows)
    try:
        png = await text_to_image(bounded, with_ansi=True, profile=profile)
        # A pathological full-color pane may still compress poorly. Remove
        # oldest rows until it fits the agreed rich-photo byte ceiling.
        while len(png) > 1024 * 1024 and len(rows) > 1:
            rows = rows[max(1, len(rows) // 8) :]
            bounded = "\n".join(rows)
            png = await text_to_image(bounded, with_ansi=True, profile=profile)
        if len(png) > 1024 * 1024:
            return None, ""
    except Exception as e:
        logger.debug("capture_pane_png: render failed: %s", e)
        return None, ""
    return png, hashlib.sha256(png).hexdigest()


def build_kb_mode_keyboard(
    user_id: int,
    window_id: str,
    ui_name: str = "",
    prompt_content: str = "",
) -> InlineKeyboardMarkup:
    """Build the kb-mode keyboard shown when the card msg is in kb-mode.

    Native model and reasoning-effort pickers become one Telegram button per option,
    followed by Cancel/Escape. Other interactive UIs retain the navigation
    grid below.

    Layout (matching the live prompt 3×3 grid):
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
        CB_ASK_PICK,
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

    picker = parse_picker_menu(prompt_content, ui_name)
    if picker is not None:
        rows = [
            [
                InlineKeyboardButton(
                    f"{'✓ ' if option.selected else ''}{option.label}"[:64],
                    callback_data=f"{CB_ASK_PICK}{index}:{window_id}"[:64],
                )
            ]
            for index, option in enumerate(picker.options)
        ]
        rows.append([kb(t(user_id, "btn.cancel"), CB_ASK_ESC)])
        return InlineKeyboardMarkup(rows)

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
