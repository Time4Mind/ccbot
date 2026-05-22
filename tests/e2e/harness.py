"""End-to-end test harness for ccbot.

These helpers drive the *real* ccbot code paths (session manager, monitor,
notifications, status-polling, callbacks) against fakes for the two external
edges the bot can't touch under test:

  * tmux — :class:`FakeTmuxManager` is an in-memory stand-in for the
    module-level ``tmux_manager`` singleton. It records ``send_keys`` calls,
    serves configurable ``capture_pane`` text, and fakes window
    list/create/kill/orphan-cleanup. Patch it onto every module that imported
    the singleton via :func:`install_fake_tmux`.
  * Telegram — :class:`FakeBot` is an ``AsyncMock``-backed object recording
    ``send_message`` / ``edit_message_text`` / ``answer_callback_query`` /
    ``send_chat_action`` etc. ``send_message`` returns a :class:`FakeMessage`
    with a monotonically-increasing ``message_id`` so the card machinery has a
    real id to edit later.

The harness also seeds Session records + ``active_sessions`` into the live
``session_manager`` (its state files are already isolated to a tmpdir by the
root ``tests/conftest.py``), writes/append JSONL transcript fixtures, and runs
the real :class:`SessionMonitor` for N deterministic poll cycles.

No production source file is modified — these fakes are wired purely via
monkeypatching the module-level references.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from ccbot.tmux_manager import TmuxWindow

# The single allowed Telegram user id — matches ALLOWED_USERS in the root
# ``tests/conftest.py``.
USER_ID = 12345


# ──────────────────────────────────────────────────────────────────────────
# Fake tmux
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class FakeTmuxManager:
    """In-memory replacement for the ``tmux_manager`` singleton.

    Windows live in ``_windows`` (window_id -> TmuxWindow). ``_panes`` maps
    window_id -> captured pane text (configurable per test). Every
    ``send_keys`` is appended to ``sent`` as ``(window_id, text, enter,
    literal)``. ``killed`` / ``orphans_killed`` record the archive teardown
    path.
    """

    session_name: str = "ccbot"
    _windows: dict[str, TmuxWindow] = field(default_factory=dict)
    _panes: dict[str, str] = field(default_factory=dict)
    sent: list[tuple[str, str, bool, bool]] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    orphans_killed: list[str] = field(default_factory=list)
    _next_wid: int = 100

    # --- test setup helpers (sync) ---

    def add_window(
        self,
        window_id: str,
        *,
        name: str = "",
        cwd: str = "/tmp",
        pane: str = "",
    ) -> TmuxWindow:
        """Register a fake window and its pane text."""
        w = TmuxWindow(
            window_id=window_id,
            window_name=name or window_id.lstrip("@"),
            cwd=cwd,
        )
        self._windows[window_id] = w
        self._panes[window_id] = pane
        return w

    def set_pane(self, window_id: str, text: str) -> None:
        """Replace the captured pane text for a window."""
        self._panes[window_id] = text

    def remove_window(self, window_id: str) -> None:
        self._windows.pop(window_id, None)
        self._panes.pop(window_id, None)

    # --- async surface mirrored from TmuxManager ---

    async def list_windows(self) -> list[TmuxWindow]:
        return list(self._windows.values())

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        return self._windows.get(window_id)

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        for w in self._windows.values():
            if w.window_name == window_name:
                return w
        return None

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        del with_ansi
        return self._panes.get(window_id)

    async def send_keys(
        self,
        window_id: str,
        text: str,
        enter: bool = True,
        literal: bool = True,
    ) -> bool:
        self.sent.append((window_id, text, enter, literal))
        return window_id in self._windows

    async def kill_window(self, window_id: str) -> bool:
        self.killed.append(window_id)
        existed = window_id in self._windows
        self.remove_window(window_id)
        return existed

    async def kill_orphan_claude_processes(self, claude_session_id: str) -> int:
        self.orphans_killed.append(claude_session_id)
        return 0

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        del start_claude, resume_session_id
        self._next_wid += 1
        wid = f"@{self._next_wid}"
        wname = window_name or Path(work_dir).name or wid.lstrip("@")
        self.add_window(wid, name=wname, cwd=work_dir)
        return True, "created", wname, wid


def install_fake_tmux(monkeypatch: Any, fake: FakeTmuxManager) -> None:
    """Patch the ``tmux_manager`` singleton reference everywhere it was
    imported ``from ..tmux_manager import tmux_manager`` (a module-level
    name binding — patching the origin module alone is not enough).
    """
    targets = [
        "ccbot.tmux_manager",
        "ccbot.session",
        "ccbot.session_monitor",
        "ccbot.session_recovery",
        "ccbot.handlers.status_polling",
        "ccbot.handlers.archive",
        "ccbot.handlers.interactive_ui",
        "ccbot.handlers.notifications",
        "ccbot.bot._common",
        "ccbot.bot.messages",
        "ccbot.bot.session_events",
        "ccbot.bot.commands.lifecycle",
        "ccbot.bot.callbacks.switcher",
    ]
    for mod in targets:
        try:
            monkeypatch.setattr(f"{mod}.tmux_manager", fake, raising=False)
        except (ImportError, AttributeError):
            # Module not imported in this test run — skip silently.
            pass


# ──────────────────────────────────────────────────────────────────────────
# Fake Telegram bot + update objects
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class FakeMessage:
    """Minimal stand-in for a sent ``telegram.Message`` — carries the
    ``message_id`` the card machinery reuses for in-place edits."""

    message_id: int
    chat_id: int
    text: str = ""
    reply_markup: Any = None


class FakeBot:
    """``AsyncMock``-backed Telegram Bot recorder.

    ``send_message`` returns an incrementing :class:`FakeMessage`; everything
    else is a plain ``AsyncMock`` so tests can assert ``.called`` /
    ``.call_args``. ``edit_message_text`` returns True (PTB returns the edited
    Message or True; the card code only checks truthiness).
    """

    def __init__(self) -> None:
        self._next_msg_id = 1000
        self.username = "Claudia_codess_bot"
        self.sent_messages: list[FakeMessage] = []
        self.edits: list[dict[str, Any]] = []

        async def _send_message(*, chat_id: int, text: str = "", **kwargs: Any):
            self._next_msg_id += 1
            msg = FakeMessage(
                message_id=self._next_msg_id,
                chat_id=chat_id,
                text=text,
                reply_markup=kwargs.get("reply_markup"),
            )
            self.sent_messages.append(msg)
            return msg

        async def _edit_message_text(
            *, chat_id: int = 0, message_id: int = 0, text: str = "", **kwargs: Any
        ):
            self.edits.append(
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "reply_markup": kwargs.get("reply_markup"),
                }
            )
            return True

        async def _send_photo(*, chat_id: int, **kwargs: Any):
            self._next_msg_id += 1
            msg = FakeMessage(message_id=self._next_msg_id, chat_id=chat_id)
            self.sent_messages.append(msg)
            return msg

        self.send_message = AsyncMock(side_effect=_send_message)
        self.edit_message_text = AsyncMock(side_effect=_edit_message_text)
        self.send_photo = AsyncMock(side_effect=_send_photo)
        self.edit_message_reply_markup = AsyncMock(return_value=True)
        self.edit_message_media = AsyncMock(return_value=True)
        self.answer_callback_query = AsyncMock(return_value=True)
        self.send_chat_action = AsyncMock(return_value=True)
        self.delete_message = AsyncMock(return_value=True)
        self.set_my_commands = AsyncMock(return_value=True)
        self.delete_my_commands = AsyncMock(return_value=True)
        self.send_document = AsyncMock(return_value=None)
        self.send_media_group = AsyncMock(return_value=None)


class FakeChat:
    """Telegram chat shortcut surface used by handlers (``send_action``)."""

    def __init__(self, chat_id: int, bot: FakeBot) -> None:
        self.id = chat_id
        self.type = "private"
        self._bot = bot

    async def send_action(self, action: Any, **kwargs: Any) -> bool:
        return await self._bot.send_chat_action(chat_id=self.id, action=action)


class FakeReplyMessage:
    """Stand-in for the inbound ``update.message`` — supports ``reply_text``
    (routes to ``bot.send_message``) and exposes ``chat`` for ``send_action``.
    """

    def __init__(
        self,
        *,
        message_id: int,
        chat_id: int,
        bot: FakeBot,
        text: str | None = None,
        reply_to_message: Any = None,
    ) -> None:
        self.message_id = message_id
        self.chat = FakeChat(chat_id, bot)
        self.text = text
        self.reply_to_message = reply_to_message
        self._bot = bot
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> FakeMessage:
        self.replies.append(text)
        return await self._bot.send_message(chat_id=self.chat.id, text=text, **kwargs)


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.is_bot = False
        self.first_name = "Tester"
        self.username = "tester"


class FakeUpdate:
    """Lightweight ``telegram.Update`` substitute for invoking handlers directly.

    PTB handlers only touch ``effective_user`` / ``message`` / ``callback_query``
    and the shortcut methods on those — all of which we provide. Driving handlers
    directly (instead of ``Application.process_update``) avoids the network calls
    PTB makes during ``Application.initialize`` while still exercising the real
    handler code top to bottom.
    """

    def __init__(
        self,
        *,
        user: FakeUser,
        message: Any = None,
        callback_query: Any = None,
    ) -> None:
        self.effective_user = user
        self.message = message
        self.callback_query = callback_query


class FakeCallbackQuery:
    """Stand-in for ``telegram.CallbackQuery`` passed to ``callback_handler``.

    Carries ``data`` + the carrier ``message`` and records ``answer`` calls.
    ``message`` mirrors enough of a Telegram Message for ``safe_edit`` (it
    reads ``message._bot`` + ``message.chat.id`` + ``message.message_id``).
    """

    def __init__(
        self,
        *,
        data: str,
        user: FakeUser,
        message_id: int,
        chat_id: int,
        bot: FakeBot,
    ) -> None:
        self.data = data
        self.from_user = user
        self.message = FakeCarrierMessage(message_id, chat_id, bot)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs: Any):
        self.answers.append((text, show_alert))
        return True

    async def edit_message_text(self, text: str, **kwargs: Any) -> bool:
        return await self.message._bot.edit_message_text(
            chat_id=self.message.chat.id,
            message_id=self.message.message_id,
            text=text,
            **kwargs,
        )


class FakeCarrierMessage:
    """The message a CallbackQuery is attached to (the carrier card)."""

    def __init__(self, message_id: int, chat_id: int, bot: FakeBot) -> None:
        self.message_id = message_id
        self.chat = FakeChat(chat_id, bot)
        self._bot = bot


# ──────────────────────────────────────────────────────────────────────────
# Session + JSONL fixtures
# ──────────────────────────────────────────────────────────────────────────


def seed_session(
    session_manager: Any,
    *,
    sid: str,
    name: str,
    window_id: str,
    workdir: str,
    claude_session_id: str = "",
    state: str = "active",
    active_for: int | None = None,
) -> Any:
    """Register a Session record (and optionally make it active) directly in
    the live ``session_manager``. Returns the Session.
    """
    from ccbot.session_models import Session

    now = time.time()
    sess = Session(
        id=sid,
        name=name,
        window_id=window_id,
        workdir=workdir,
        state=state,  # type: ignore[arg-type]
        claude_session_id=claude_session_id,
        created_at=now,
        last_event_at=now,
    )
    session_manager.sessions[sid] = sess
    if window_id:
        ws = session_manager.get_window_state(window_id)
        ws.cwd = workdir
        if claude_session_id:
            ws.session_id = claude_session_id
        if name:
            session_manager.window_display_names[window_id] = name
    if active_for is not None:
        session_manager.active_sessions[active_for] = sid
    return sess


def write_session_map(
    session_map_file: Path,
    *,
    window_id: str,
    claude_session_id: str,
    cwd: str,
    session_name: str = "ccbot",
    window_name: str = "",
) -> None:
    """Write a session_map.json entry keyed ``<session_name>:<window_id>`` —
    the exact shape the SessionStart/UserPromptSubmit hook produces."""
    data: dict[str, Any] = {}
    if session_map_file.exists():
        try:
            data = json.loads(session_map_file.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    data[f"{session_name}:{window_id}"] = {
        "session_id": claude_session_id,
        "cwd": cwd,
        "window_name": window_name,
    }
    session_map_file.write_text(json.dumps(data))


def make_jsonl_path(projects_path: Path, cwd: str, claude_session_id: str) -> Path:
    """Compute (and mkdir) the JSONL fixture path for a session, matching
    Claude Code's ``<projects>/<encoded_cwd>/<sid>.jsonl`` convention."""
    from ccbot.session_claude_io import encode_cwd

    proj_dir = projects_path / encode_cwd(cwd)
    proj_dir.mkdir(parents=True, exist_ok=True)
    return proj_dir / f"{claude_session_id}.jsonl"


def assistant_turn(text: str, *, stop_reason: str = "end_turn") -> dict[str, Any]:
    """A complete assistant end-of-turn JSONL entry."""
    return {
        "type": "assistant",
        "timestamp": "2026-05-23T00:00:00.000Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def user_turn(text: str, *, cwd: str = "/tmp") -> dict[str, Any]:
    """A user JSONL entry. ``cwd`` must match the session workdir — the
    monitor uses it to bind an un-indexed JSONL to its tmux window's cwd."""
    return {
        "type": "user",
        "timestamp": "2026-05-23T00:00:00.000Z",
        "cwd": cwd,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
