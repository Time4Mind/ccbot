"""Named Telegram choices backed by the complete native OpenCode catalogue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import secrets
import re
from collections.abc import Callable
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .i18n import t
from .opencode_catalogue import ModelChoice, load_catalogue
from .opencode_runtime import composer_identity, interactive_panel, is_ready
from .session_models import Session
from .terminal_runtime import PaneCaptureError, capture_session_pane, send_session_key
from .tmux_manager import tmux_manager

PREFIX = "oc:pick:"
CANCEL = "oc:cancel:"
PAGE = "oc:page:"
UI_NAME = "OpenCodePicker"
PAGE_SIZE = 40


@dataclass
class Picker:
    user_id: int
    session_id: str
    window_id: str
    models: tuple[ModelChoice, ...]
    identity: tuple[str, str] | None
    phase: str = "model"
    model: ModelChoice | None = None
    token: str = field(default_factory=lambda: secrets.token_hex(6))
    page: int = 0
    busy: bool = False
    created: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def labels(self) -> tuple[str, ...]:
        if self.phase == "variant" and self.model:
            return ("Default", *self.model.variants)
        return tuple(m.label for m in self.models)

    @property
    def prompt(self) -> str:
        title = (
            "Select model"
            if self.phase == "model"
            else f"Select reasoning effort · {self.model.name if self.model else ''}"
        )
        active = " / ".join(self.identity) if self.identity else "—"
        start = self.page * PAGE_SIZE
        pages = (len(self.labels) + PAGE_SIZE - 1) // PAGE_SIZE
        return (
            f"{title}\nCurrent model: {active}\n{len(self.labels)} options · {self.page + 1}/{pages}\n"
            + "\n".join(
                f"{i + 1}. {label[:70]}"
                for i, label in enumerate(self.labels[start : start + PAGE_SIZE], start)
            )
        )


_pickers: dict[tuple[int, str], Picker] = {}
_prepare_locks: dict[tuple[int, str], asyncio.Lock] = {}


def discard(user_id: int, window_id: str) -> None:
    picker = _pickers.get((user_id, window_id))
    if picker and not picker.busy:
        _pickers.pop((user_id, window_id), None)


def switching(sess: Session | None) -> bool:
    return bool(
        sess and any(p.session_id == sess.id and p.busy for p in _pickers.values())
    )


async def prepare_picker(
    user_id: int, sess: Session, content: str, name: str
) -> tuple[str, str]:
    if sess.backend != "opencode" or not (
        name == "OpenCodeVariant"
        or (name == "Settings" and "Select model" in content and "ctrl+a" in content)
    ):
        return content, name
    key = (user_id, sess.window_id)
    async with _prepare_locks.setdefault(key, asyncio.Lock()):
        picker = _pickers.get(key)
        phase = "variant" if name == "OpenCodeVariant" else "model"
        if (
            picker
            and picker.session_id == sess.id
            and time.monotonic() - picker.created < 900
        ):
            if picker.busy or picker.phase == phase:
                return picker.prompt, UI_NAME
        try:
            models = (
                picker.models
                if picker and picker.session_id == sess.id
                else await load_catalogue(sess)
            )
            pane = await capture_session_pane(sess)
            identity = model_identity(pane, models)
            model = next(
                (m for m in models if identity and m.name == identity[0]), None
            )
            if phase == "variant" and model is None:
                raise ValueError("Current OpenCode model is not in its catalogue")
            picker = Picker(
                user_id,
                sess.id,
                sess.window_id,
                models,
                identity,
                phase=phase,
                model=model,
            )
            _pickers[key] = picker
            return picker.prompt, UI_NAME
        except (OSError, ValueError, TimeoutError, PaneCaptureError):
            _pickers.pop(key, None)
            return (
                "Не удалось прочитать модели OpenCode. Закройте селектор и повторите /model.",
                UI_NAME,
            )


def keyboard(
    user_id: int, window_id: str, prompt: str, name: str
) -> InlineKeyboardMarkup | None:
    if name != UI_NAME:
        return None
    picker = _pickers.get((user_id, window_id))
    if picker is None or picker.prompt != prompt:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t(user_id, "btn.cancel"), callback_data=f"aq:esc:{window_id}"
                    )
                ]
            ]
        )
    rows = []
    start = picker.page * PAGE_SIZE
    for i, label in enumerate(picker.labels[start : start + PAGE_SIZE], start):
        selected = False
        if picker.identity:
            selected = (
                picker.identity[1] == ("default" if label == "Default" else label)
                if picker.phase == "variant"
                else picker.models[i].name == picker.identity[0]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    (("✓ " if selected else "") + label)[:64],
                    callback_data=f"{PREFIX}{picker.token}:{i}",
                )
            ]
        )
    pages = (len(picker.labels) + PAGE_SIZE - 1) // PAGE_SIZE
    if pages > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Страница {i + 1}", callback_data=f"{PAGE}{picker.token}:{i}"
                )
                for i in range(pages)
                if i != picker.page
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t(user_id, "btn.cancel"), callback_data=f"{CANCEL}{picker.token}"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def resolve(user_id: int, token: str) -> Picker | None:
    return next(
        (
            p
            for p in _pickers.values()
            if p.user_id == user_id
            and p.token == token
            and time.monotonic() - p.created < 900
        ),
        None,
    )


async def _key(sess: Session, key: str) -> None:
    if not await send_session_key(sess, key):
        raise ValueError("OpenCode did not receive the selection")


async def _text(sess: Session, value: str) -> None:
    if sess.node_id != "local" or not await tmux_manager.send_keys(
        sess.window_id, value, enter=False, literal=True
    ):
        raise ValueError("OpenCode did not receive the picker filter")


async def _wait(sess: Session, predicate: Callable[[str], bool]) -> str:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        pane = await capture_session_pane(sess)
        if predicate(pane):
            return pane
        await asyncio.sleep(0.05)
    raise ValueError("OpenCode did not confirm the selection; reopen /model")


def _filtered_first(content: str, labels: tuple[str, ...], expected: str) -> bool:
    # The first nonempty row after the title is the search input, not an option.
    rows = [x.strip().lstrip("●◉›> ") for x in content.splitlines() if x.strip()]
    if len(rows) < 2 or rows[1] != expected:
        return False
    for row in rows[2:]:
        title = row.split("  ", 1)[0].removesuffix(" (Favorite)").strip()
        if title in labels:
            return title == expected
    return False


def model_identity(
    pane: str, models: tuple[ModelChoice, ...]
) -> tuple[str, str] | None:
    # Match known model names longest first, so a base model never absorbs Fast/Flex.
    for line in reversed(pane.splitlines()[-16:]):
        match = re.match(r"^\s*┃\s+(?:Build|Plan)\s+·\s+(.+)$", line)
        if not match:
            continue
        for model in sorted(models, key=lambda m: -len(m.name)):
            if match[1].startswith(model.name + " "):
                tail = match[1][len(model.name) :].strip()
                mode = next(
                    (
                        v
                        for v in model.variants
                        if tail.endswith(" · " + v) or tail.startswith(v + " ")
                    ),
                    "default",
                )
                return model.name, mode
    return composer_identity(pane)


async def choose(picker: Picker, sess: Session, index: int, token: str) -> None:
    async with picker.lock:
        if (
            picker.token != token
            or resolve(picker.user_id, token) is not picker
            or not 0 <= index < len(picker.labels)
        ):
            raise ValueError("Этот селектор устарел. Откройте /model заново.")
        pane = await capture_session_pane(sess)
        panel = interactive_panel(pane)
        expected_panel = "Settings" if picker.phase == "model" else "OpenCodeVariant"
        if panel is None or panel[0] != expected_panel:
            raise ValueError("Этот селектор уже закрыт. Откройте /model заново.")
        picker.busy = True
        try:
            label = (
                picker.models[index].name
                if picker.phase == "model"
                else picker.labels[index]
            )
            if (
                picker.phase == "model"
                and sum(m.name == label for m in picker.models) != 1
            ):
                raise ValueError(
                    "Название модели неоднозначно между провайдерами; выбор не отправлен."
                )
            await _key(sess, "C-u")
            await _text(sess, label)
            labels = (
                tuple(m.name for m in picker.models)
                if picker.phase == "model"
                else picker.labels
            )
            await _wait(
                sess,
                lambda p: bool(
                    (v := interactive_panel(p))
                    and v[0] == expected_panel
                    and _filtered_first(v[1], labels, label)
                ),
            )
            await _key(sess, "Home")
            await _key(sess, "Enter")
            if picker.phase == "model":
                model = picker.models[index]
                pane = await _wait(
                    sess,
                    lambda p: bool(
                        (identity := model_identity(p, picker.models))
                        and identity[0] == model.name
                        and (
                            is_ready(p)
                            or (v := interactive_panel(p))
                            and v[0] == "OpenCodeVariant"
                        )
                    ),
                )
                if model.variants:
                    if interactive_panel(pane) is None:
                        await _text(sess, "/variants")
                        await _key(sess, "Enter")
                        pane = await _wait(
                            sess,
                            lambda p: bool(
                                (v := interactive_panel(p))
                                and v[0] == "OpenCodeVariant"
                            ),
                        )
                    picker.model = model
                    picker.phase = "variant"
                    picker.identity = model_identity(pane, picker.models)
                    picker.token = secrets.token_hex(6)
                    picker.page = 0
                else:
                    _pickers.pop((picker.user_id, picker.window_id), None)
            else:
                expected_effort = "default" if label == "Default" else label
                await _wait(
                    sess,
                    lambda p: (
                        is_ready(p)
                        and bool(
                            (identity := model_identity(p, picker.models))
                            and identity[1] == expected_effort
                        )
                    ),
                )
                _pickers.pop((picker.user_id, picker.window_id), None)
        finally:
            picker.busy = False
