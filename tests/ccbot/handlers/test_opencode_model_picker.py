"""OpenCode exposes the full catalogue and explicit modes as named buttons."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.card_types import CardState
from ccbot.handlers.kb_mode import build_kb_mode_keyboard
from ccbot.session_models import Session

PANEL = """Select model                     esc
 Search
 Recent
● GPT-6 Luna OpenAI
 GPT-6.1 Sol OpenAI
 Connect provider ctrl+a  Favorite ctrl+f"""
PANE = PANEL + "\n┃\n┃ Build · GPT-6 Luna OpenAI\n/tmp/lab ctrl+p commands"
MODELS = [
    {
        "id": "gpt-6-luna",
        "providerID": "openai",
        "name": "GPT-6 Luna",
        "variants": {"low": {}, "high": {}},
    },
    {
        "id": "gpt-6.1-sol",
        "providerID": "openai",
        "name": "GPT-6.1 Sol",
        "variants": {"low": {}, "medium": {}, "high": {}, "max": {}},
    },
    {
        "id": "gpt-6.1-terra",
        "providerID": "openai",
        "name": "GPT-6.1 Terra",
        "variants": {"low": {}, "high": {}},
    },
]


@pytest.mark.asyncio
async def test_card_shows_full_catalogue_not_only_visible_recents(monkeypatch):
    import asyncio
    from ccbot.handlers import card_kb_state
    from ccbot import terminal_runtime, opencode_picker

    opencode_picker._pickers.clear()
    monkeypatch.setattr(
        opencode_picker, "capture_session_pane", AsyncMock(return_value=PANE)
    )

    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(
                "".join(
                    f"{x['providerID']}/{x['id']}\n{json.dumps(x)}\n" for x in MODELS
                ).encode(),
                b"",
            )
        ),
    )
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(
        terminal_runtime, "capture_session_pane", AsyncMock(return_value=PANE)
    )
    sess = Session(
        id="oc-model-test",
        name="test",
        window_id="@oc-test",
        workdir="/tmp",
        backend="opencode",
    )
    state = CardState(msg_id=123, agent_model="GPT-6 Luna", reasoning_effort="default")
    monkeypatch.setattr(card_kb_state, "get_card_state", lambda *_: state)
    edit = AsyncMock()
    monkeypatch.setattr(notifications, "_edit_card", edit)
    await notifications.enter_kb_mode(AsyncMock(), 42, sess, PANEL, "Settings")
    kb = edit.await_args.kwargs["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("GPT-6.1 Terra" in x for x in labels), labels
    assert not any(x in labels for x in ["↑", "↓", "←", "→", "␣ Space", "⏎ Enter"])
    assert any("GPT-6 Luna" in x and "✓" in x for x in labels)


def test_native_variant_has_named_mode_buttons_without_auto_default():
    kb = build_kb_mode_keyboard(
        42,
        "@variant",
        ui_name="OpenCodeVariant",
        prompt_content="Select variant esc\nSearch\nDefault\nlow\nmedium\n● high\nmax",
    )
    assert [r[0].text for r in kb.inline_keyboard] == [
        "Default",
        "low",
        "medium",
        "✓ high",
        "max",
        "× Cancel",
    ]


@pytest.mark.asyncio
async def test_direct_variant_menu_shows_native_modes_when_composer_is_covered(
    monkeypatch,
):
    from ccbot import opencode_picker
    from ccbot.opencode_catalogue import ModelChoice
    from ccbot.terminal_parser import extract_interactive_content

    pane = """              Select variant                                   esc
              Search
              ● Default
  ┃           low
  ┃  Buil     high
  /tmp/lab  ⊙ 1 MCP /status                               1.19.5
"""
    panel = extract_interactive_content(pane)
    assert panel is not None
    sess = Session(
        id="oc-hidden-composer-variant",
        name="test",
        window_id="@oc-hidden-variant",
        workdir="/tmp",
        backend="opencode",
    )
    model = ModelChoice("openai", "fixture", "Fixture", ("low", "high", "xhigh"))
    monkeypatch.setattr(
        opencode_picker, "load_catalogue", AsyncMock(return_value=(model,))
    )
    monkeypatch.setattr(
        opencode_picker, "capture_session_pane", AsyncMock(return_value=pane)
    )
    content, ui = await opencode_picker.prepare_picker(
        42, sess, panel.content, panel.name
    )
    kb = opencode_picker.keyboard(42, sess.window_id, content, ui)
    assert kb is not None
    assert [
        button.text
        for row in kb.inline_keyboard
        for button in row
        if button.callback_data.startswith("oc:pick:")
    ] == ["Default", "low", "high"]


class NativePicker:
    """Native dialogue behavior: filtering, model/mode selection, and command input."""

    def __init__(self, models, automatic_variant=True):
        self.models = models
        self.model = models[0]
        self.mode = "default"
        self.dialog = "model"
        self.filter = ""
        self.automatic_variant = automatic_variant
        self.hide_variant_composer = False
        self.submitted_prompts = []

    async def capture(self, _sess):
        footer = f"\n┃\n┃ Build · {self.model.name} OpenAI{' · ' + self.mode if self.mode != 'default' else ''}\n/tmp/lab ctrl+p commands"
        if self.dialog == "model":
            labels = [
                m.name for m in self.models if self.filter.lower() in m.name.lower()
            ]
            return (
                "Select model esc\n"
                + (self.filter or "Search")
                + "\nOpenAI\n"
                + "\n".join(labels)
                + "\nConnect provider ctrl+a"
                + footer
            )
        if self.dialog == "variant":
            labels = [
                v
                for v in ("Default", *self.model.variants)
                if self.filter.lower() in v.lower()
            ]
            return (
                "Select variant esc\n"
                + (self.filter or "Search")
                + "\n"
                + "\n".join(labels)
                + (
                    "\n/tmp/lab  ⊙ 1 MCP /status                    1.19.5"
                    if self.hide_variant_composer
                    else footer
                )
            )
        return footer

    async def text(self, _sess, text):
        self.filter = text

    async def key(self, _sess, key):
        if key == "C-u":
            self.filter = ""
        if key == "Escape":
            self.dialog = ""
            self.filter = ""
        if key == "Enter":
            if self.dialog == "model":
                self.model = next(m for m in self.models if self.filter == m.name)
                self.dialog = "variant" if self.automatic_variant else ""
                self.filter = ""
            elif self.dialog == "variant":
                self.mode = "default" if self.filter == "Default" else self.filter
                self.dialog = ""
                self.filter = ""
            elif self.filter == "/variants":
                self.dialog = "variant"
                self.filter = ""
            else:
                self.submitted_prompts.append(self.filter)
        return True


@pytest.fixture
def native_picker(monkeypatch):
    from ccbot import opencode_picker as p
    from ccbot.opencode_catalogue import parse_catalogue
    from ccbot.bot.callbacks import interactive_ui, opencode_picker as callbacks

    models = parse_catalogue(
        "".join(f"{x['providerID']}/{x['id']}\n{json.dumps(x)}\n" for x in MODELS)
    )
    p._pickers.clear()
    native = NativePicker(models)
    sess = Session(
        id="oc-choices",
        name="test",
        window_id="@choices",
        workdir="/tmp",
        backend="opencode",
    )
    monkeypatch.setattr(p, "load_catalogue", AsyncMock(return_value=models))
    monkeypatch.setattr(p, "capture_session_pane", native.capture)
    monkeypatch.setattr(p, "send_session_key", native.key)
    monkeypatch.setattr(p, "_text", native.text)
    monkeypatch.setattr(callbacks, "capture_session_pane", native.capture)
    monkeypatch.setattr(callbacks, "send_session_key", native.key)
    monkeypatch.setattr(callbacks.session_manager, "get_active_session", lambda _: sess)
    monkeypatch.setattr(interactive_ui, "_refresh_after_key", AsyncMock())
    yield p, native, sess, interactive_ui
    p._pickers.clear()


async def named_callback(p, native, sess, interactive, label):
    panel = native.dialog
    pane = await native.capture(sess)
    from ccbot.opencode_runtime import interactive_panel

    detected = interactive_panel(pane)
    assert detected
    content, name = await p.prepare_picker(42, sess, detected[1], detected[0])
    kb = p.keyboard(42, sess.window_id, content, name)
    assert kb
    button = next(b for row in kb.inline_keyboard for b in row if label in b.text)
    query = SimpleNamespace(data=button.callback_data, answer=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await interactive.handle(
        query, SimpleNamespace(bot=bot), SimpleNamespace(id=42)
    )
    assert not bot.send_message.await_args_list, (
        panel,
        bot.send_message.await_args_list,
    )
    return button.callback_data


@pytest.mark.asyncio
async def test_model_selection_is_verified_when_variant_hides_composer(native_picker):
    p, native, sess, interactive = native_picker
    native.hide_variant_composer = True

    await named_callback(p, native, sess, interactive, "GPT-6.1 Terra")
    assert native.dialog == "variant"
    await named_callback(p, native, sess, interactive, "high")

    assert (native.model.id, native.mode) == ("gpt-6.1-terra", "high")
    assert not native.submitted_prompts


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic_variant", [True, False])
async def test_model_and_explicit_high_mode_are_applied_without_extra_prompt(
    native_picker, automatic_variant
):
    p, native, sess, interactive = native_picker
    native.automatic_variant = automatic_variant
    await named_callback(p, native, sess, interactive, "GPT-6.1 Terra")
    assert native.model.id == "gpt-6.1-terra" and native.dialog == "variant"
    assert native.mode == "default"  # Merely opening the mode chooser never answers it.
    await named_callback(p, native, sess, interactive, "high")
    assert native.mode == "high" and native.dialog == ""
    assert native.submitted_prompts == []


@pytest.mark.asyncio
async def test_repeated_model_callback_cannot_become_a_mode_choice(native_picker):
    import asyncio

    p, native, sess, interactive = native_picker
    content, name = await p.prepare_picker(42, sess, PANEL, "Settings")
    kb = p.keyboard(42, sess.window_id, content, name)
    button = next(
        b for row in kb.inline_keyboard for b in row if "GPT-6.1 Terra" in b.text
    )

    async def click():
        await interactive.handle(
            SimpleNamespace(data=button.callback_data, answer=AsyncMock()),
            SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock())),
            SimpleNamespace(id=42),
        )

    await asyncio.gather(click(), click())
    assert (
        native.dialog == "variant"
        and native.mode == "default"
        and native.submitted_prompts == []
    )


@pytest.mark.asyncio
async def test_cancel_never_approves_an_unrelated_permission(native_picker):
    p, native, sess, interactive = native_picker
    content, name = await p.prepare_picker(42, sess, PANEL, "Settings")
    kb = p.keyboard(42, sess.window_id, content, name)
    cancel = kb.inline_keyboard[-1][0].callback_data
    native.dialog = "permission"
    # A stale cancellation must not press Enter/escape into another native operation.
    await interactive.handle(
        SimpleNamespace(data=cancel, answer=AsyncMock()),
        SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock())),
        SimpleNamespace(id=42),
    )
    assert native.dialog == "permission" and native.submitted_prompts == []


@pytest.mark.asyncio
async def test_old_keyboard_and_other_user_do_not_control_current_session(
    native_picker,
):
    p, native, sess, interactive = native_picker
    content, name = await p.prepare_picker(42, sess, PANEL, "Settings")
    kb = p.keyboard(42, sess.window_id, content, name)
    data = kb.inline_keyboard[0][0].callback_data
    query = SimpleNamespace(data=data, answer=AsyncMock())
    await interactive.handle(
        query,
        SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock())),
        SimpleNamespace(id=99),
    )
    assert native.model.id == "gpt-6-luna" and native.dialog == "model"
    p.discard(42, sess.window_id)
    await interactive.handle(
        query,
        SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock())),
        SimpleNamespace(id=42),
    )
    assert native.dialog == "model" and not native.submitted_prompts


@pytest.mark.asyncio
async def test_catalogue_pages_keep_named_buttons_and_refresh_card(
    native_picker, monkeypatch
):
    from ccbot.opencode_catalogue import ModelChoice

    p, native, sess, interactive = native_picker
    models = tuple(
        ModelChoice("openai", f"id-{i}", f"Model {i}", ("low", "high"))
        for i in range(87)
    )
    native.models = models
    native.model = models[0]
    monkeypatch.setattr(p, "load_catalogue", AsyncMock(return_value=models))
    content, name = await p.prepare_picker(42, sess, PANEL, "Settings")
    keyboard = p.keyboard(42, sess.window_id, content, name)
    assert keyboard
    assert len(keyboard.inline_keyboard) < 100
    page = next(
        b for row in keyboard.inline_keyboard for b in row if b.text == "Страница 3"
    )
    await interactive.handle(
        SimpleNamespace(data=page.callback_data, answer=AsyncMock()),
        SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock())),
        SimpleNamespace(id=42),
    )
    updated, name = await p.prepare_picker(42, sess, PANEL, "Settings")
    assert content != updated
    labels = [
        b.text
        for row in p.keyboard(42, sess.window_id, updated, name).inline_keyboard
        for b in row
    ]
    assert "Model 86 · openai" in labels and "Model 0 · openai" not in labels
    assert native.dialog == "model" and native.submitted_prompts == []


@pytest.mark.asyncio
async def test_catalogue_failure_has_cancel_and_never_falls_back_to_arrows(
    native_picker, monkeypatch
):
    p, native, sess, interactive = native_picker
    monkeypatch.setattr(
        p, "load_catalogue", AsyncMock(side_effect=ValueError("unavailable"))
    )
    content, name = await p.prepare_picker(42, sess, PANEL, "Settings")
    labels = [
        b.text
        for row in p.keyboard(42, sess.window_id, content, name).inline_keyboard
        for b in row
    ]
    assert labels == ["× Cancel"] and "/model" in content


def test_catalogue_uses_available_native_models_and_rejects_bad_records():
    from ccbot.opencode_catalogue import parse_catalogue

    source = [
        *MODELS,
        MODELS[0] | {"id": "retired", "status": "deprecated"},
        MODELS[0] | {"providerID": "opencode", "id": "foo-nano"},
        MODELS[0] | {"id": "broken", "name": "bad\nname"},
    ]
    models = parse_catalogue(
        "".join(
            f"{x['providerID']}/{x['id']}\n{json.dumps(x, indent=2)}\n" for x in source
        )
    )
    assert {m.id for m in models} == {"gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-terra"}
    assert next(m for m in models if m.id == "gpt-6.1-sol").variants == (
        "low",
        "medium",
        "high",
        "max",
    )


@pytest.mark.asyncio
async def test_pending_prompt_waits_for_explicit_mode_then_resumes(
    native_picker, monkeypatch
):
    import asyncio
    from ccbot.bot import messages

    p, native, sess, interactive = native_picker
    await named_callback(p, native, sess, interactive, "GPT-6.1 Terra")
    manager = SimpleNamespace(
        find_session_by_window=lambda _: sess, get_active_session=lambda _: sess
    )
    monkeypatch.setattr(messages, "session_manager", manager)
    monkeypatch.setattr(
        messages,
        "tmux_manager",
        SimpleNamespace(
            find_window_by_id=AsyncMock(
                return_value=SimpleNamespace(window_id=sess.window_id)
            ),
            capture_pane=AsyncMock(side_effect=lambda *_a, **_k: native.capture(sess)),
        ),
    )

    async def pane(*_a, **_k):
        return await native.capture(sess)

    monkeypatch.setattr(messages, "capture_window_pane", pane)
    surfaced = asyncio.Event()

    async def keyboard(*_a, **_k):
        surfaced.set()

    monkeypatch.setattr(messages, "enter_kb_mode", keyboard)
    pending = asyncio.create_task(
        messages._intercept_if_pending_ui(
            AsyncMock(), 42, sess.window_id, SimpleNamespace(), wait_until_clear=True
        )
    )
    await asyncio.wait_for(surfaced.wait(), 1)
    assert not pending.done() and native.dialog == "variant"
    await named_callback(p, native, sess, interactive, "high")
    assert await asyncio.wait_for(pending, 1) is False
    assert native.mode == "high" and native.submitted_prompts == []


@pytest.mark.asyncio
async def test_medium_choice_works_when_old_user_block_shares_dialog_rows(
    native_picker, monkeypatch
):
    p, native, sess, interactive = native_picker
    await named_callback(p, native, sess, interactive, "GPT-6.1 Sol")
    original_capture = native.capture

    async def with_history(session):
        text = await original_capture(session)
        if native.dialog != "variant":
            return text
        lines = text.splitlines()
        prefix = []
        for line in lines:
            if line.startswith("┃") or line.startswith("/tmp"):
                prefix.append(line)
            elif line:
                prefix.append(
                    (
                        "     ▣  Bu    "
                        if line.startswith("Select variant")
                        else "  ┃           "
                    )
                    + line
                )
            else:
                prefix.append(line)
        return "\n".join(prefix)

    monkeypatch.setattr(p, "capture_session_pane", with_history)
    await named_callback(p, native, sess, interactive, "medium")
    assert (
        native.mode == "medium"
        and native.dialog == ""
        and native.submitted_prompts == []
    )


@pytest.mark.asyncio
async def test_reopened_mode_buttons_mark_actual_medium_after_provider(native_picker):
    p, native, sess, interactive = native_picker
    await named_callback(p, native, sess, interactive, "GPT-6.1 Sol")
    await named_callback(p, native, sess, interactive, "medium")
    native.dialog = "model"
    await named_callback(p, native, sess, interactive, "GPT-6.1 Sol")
    pane = await native.capture(sess)
    from ccbot.opencode_runtime import interactive_panel

    panel = interactive_panel(pane)
    assert panel
    content, name = await p.prepare_picker(42, sess, panel[1], panel[0])
    kb = p.keyboard(42, sess.window_id, content, name)
    assert any(b.text == "✓ medium" for row in kb.inline_keyboard for b in row)
