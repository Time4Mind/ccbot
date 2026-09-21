from __future__ import annotations

import asyncio

import pytest

from ccbot.codex_startup import (
    CodexStartupError,
    drive_codex_startup,
    is_codex_ready,
)


UPDATE_PROMPT = """Update available! 0.147.0 -> 0.151.0
› 1. Update now
  2. Skip
  3. Skip until next version
Press enter to continue
"""

READY_PROMPT = """OpenAI Codex

› Ask anything

gpt-5.6 medium · ~/project
"""

HOOKS_REVIEW_PROMPT = """Hooks need review
2 hooks are new or changed.
Hooks can run outside the sandbox after you trust them.

1. Review hooks
2. Trust all and continue
3. Continue without trusting (hooks won't run)
"""


@pytest.mark.asyncio
async def test_delayed_update_is_handled_before_readiness_is_final() -> None:
    screens = [
        READY_PROMPT,
        READY_PROMPT,
        UPDATE_PROMPT,
        "Installing update",
        "shell",
    ]
    processes = ["codex", "codex", "codex", "npm", "zsh"]
    keys: list[str] = []
    relaunched: list[str] = []

    async def capture() -> str:
        return screens.pop(0) if screens else READY_PROMPT

    async def current_process() -> str:
        return processes.pop(0) if processes else "codex"

    result = await drive_codex_startup(
        command="codex --no-alt-screen",
        capture=capture,
        current_process=current_process,
        send_key=lambda key: _append(keys, key),
        relaunch=lambda command: _append(relaunched, command),
        timeout=7,
        poll_interval=0.001,
    )

    assert result.updated is True
    assert keys == ["ENTER"]
    assert relaunched == ["codex --no-alt-screen"]


@pytest.mark.asyncio
async def test_update_is_installed_then_exact_command_is_relaunched() -> None:
    screens = iter([UPDATE_PROMPT, UPDATE_PROMPT, "shell", READY_PROMPT])
    processes = iter(["codex", "npm", "zsh", "codex"])
    keys: list[str] = []
    relaunched: list[str] = []

    async def capture() -> str:
        return next(screens)

    async def current_process() -> str:
        return next(processes)

    async def send_key(key: str) -> None:
        keys.append(key)

    async def relaunch(command: str) -> None:
        relaunched.append(command)

    result = await drive_codex_startup(
        command="env CCBOT_INTERFACE=telegram codex --no-alt-screen resume abc",
        capture=capture,
        current_process=current_process,
        send_key=send_key,
        relaunch=relaunch,
        timeout=1,
        poll_interval=0,
        ready_settle_time=0,
    )

    assert result.updated is True
    assert keys == ["ENTER"]
    assert relaunched == [
        "env CCBOT_INTERFACE=telegram codex --no-alt-screen resume abc"
    ]


@pytest.mark.asyncio
async def test_repeated_update_prompt_after_relaunch_is_bounded_failure() -> None:
    screens = iter([UPDATE_PROMPT, "shell", READY_PROMPT, UPDATE_PROMPT])
    processes = iter(["codex", "zsh", "codex", "codex"])

    with pytest.raises(CodexStartupError, match="repeated update prompt"):
        await drive_codex_startup(
            command="codex",
            capture=lambda: _next(screens),
            current_process=lambda: _next(processes),
            send_key=lambda _key: _done(),
            relaunch=lambda _command: _done(),
            timeout=1,
            poll_interval=0.001,
            ready_settle_time=0.02,
        )


@pytest.mark.asyncio
async def test_trust_and_resume_directory_prompts_use_the_same_lifecycle() -> None:
    screens = iter(
        [
            "Do you trust the contents of this directory?\n› 1. Yes, continue",
            "Choose working directory to resume this session\n"
            "› 1. Use session directory\n"
            "  2. Use current directory\n"
            "Press enter to continue",
            READY_PROMPT,
        ]
    )
    keys: list[str] = []

    result = await drive_codex_startup(
        command="codex resume abc",
        capture=lambda: _next(screens),
        current_process=lambda: _value("codex"),
        send_key=lambda key: _append(keys, key),
        relaunch=lambda _command: _done(),
        timeout=1,
        poll_interval=0,
        ready_settle_time=0,
    )

    assert result.updated is False
    assert keys == ["ENTER", "DOWN", "ENTER"]


@pytest.mark.asyncio
async def test_hooks_review_trusts_worker_hooks_and_reaches_composer() -> None:
    screens = iter([HOOKS_REVIEW_PROMPT, READY_PROMPT])
    keys: list[str] = []

    result = await drive_codex_startup(
        command="codex",
        capture=lambda: _next(screens),
        current_process=lambda: _value("codex"),
        send_key=lambda key: _append(keys, key),
        relaunch=lambda _command: _done(),
        timeout=1,
        poll_interval=0,
        ready_settle_time=0,
    )

    assert result.updated is False
    assert keys == ["DOWN", "ENTER"]


@pytest.mark.asyncio
async def test_authentication_modal_fails_without_waiting_for_timeout() -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()

    with pytest.raises(CodexStartupError, match="authentication"):
        await drive_codex_startup(
            command="codex",
            capture=lambda: _value("Sign in with ChatGPT\nSign in with device code"),
            current_process=lambda: _value("codex"),
            send_key=lambda _key: _done(),
            relaunch=lambda _command: _done(),
            timeout=10,
            poll_interval=0.1,
            ready_settle_time=0,
        )

    assert loop.time() - started < 1


@pytest.mark.asyncio
async def test_unknown_numbered_startup_modal_fails_promptly() -> None:
    with pytest.raises(CodexStartupError, match="unsupported startup prompt"):
        await drive_codex_startup(
            command="codex",
            capture=lambda: _value(
                "Unknown startup choice\n"
                "› 1. First option\n"
                "  2. Second option\n"
                "Press enter to continue"
            ),
            current_process=lambda: _value("codex"),
            send_key=lambda _key: _done(),
            relaunch=lambda _command: _done(),
            timeout=10,
            poll_interval=0.1,
            ready_settle_time=0,
        )


@pytest.mark.asyncio
async def test_initial_shell_render_race_does_not_fail_startup() -> None:
    screens = iter(["shell", READY_PROMPT])
    processes = iter(["zsh", "codex"])

    result = await drive_codex_startup(
        command="codex",
        capture=lambda: _next(screens),
        current_process=lambda: _next(processes),
        send_key=lambda _key: _done(),
        relaunch=lambda _command: _done(),
        timeout=1,
        poll_interval=0,
        ready_settle_time=0,
    )

    assert result.updated is False


@pytest.mark.asyncio
async def test_stable_readiness_completes_within_bounded_settle_window() -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()

    result = await drive_codex_startup(
        command="codex",
        capture=lambda: _value(READY_PROMPT),
        current_process=lambda: _value("codex"),
        send_key=lambda _key: _done(),
        relaunch=lambda _command: _done(),
        timeout=0.5,
        poll_interval=0.001,
        ready_settle_time=0.02,
    )

    elapsed = loop.time() - started
    assert result.updated is False
    assert 0.02 <= elapsed < 0.5


@pytest.mark.asyncio
async def test_startup_prompts_reset_readiness_settle_window() -> None:
    trust = "Do you trust the contents of this directory?\n› 1. Yes, continue"
    resume = (
        "Choose working directory to resume this session\n"
        "› 1. Use session directory\n"
        "  2. Use current directory\n"
        "Press enter to continue"
    )
    screens = [
        READY_PROMPT,
        READY_PROMPT,
        trust,
        READY_PROMPT,
        READY_PROMPT,
        resume,
        READY_PROMPT,
        READY_PROMPT,
        "Running startup hooks",
    ]
    keys: list[str] = []
    loop = asyncio.get_running_loop()
    last_non_ready_at: float | None = None

    async def capture() -> str:
        nonlocal last_non_ready_at
        screen = screens.pop(0) if screens else READY_PROMPT
        if screen == "Running startup hooks":
            last_non_ready_at = loop.time()
        return screen

    await drive_codex_startup(
        command="codex resume abc",
        capture=capture,
        current_process=lambda: _value("codex"),
        send_key=lambda key: _append(keys, key),
        relaunch=lambda _command: _done(),
        timeout=1,
        poll_interval=0.005,
        ready_settle_time=0.05,
    )

    assert keys == ["ENTER", "DOWN", "ENTER"]
    assert last_non_ready_at is not None
    assert loop.time() - last_non_ready_at >= 0.05


def test_modal_selector_is_not_codex_readiness() -> None:
    assert is_codex_ready(UPDATE_PROMPT) is False
    assert (
        is_codex_ready(
            "Choose working directory to resume this session\n"
            "› 1. Use session directory\n"
            "  2. Use current directory\n"
            "Press enter to continue"
        )
        is False
    )
    assert is_codex_ready(READY_PROMPT) is True


async def _next(iterator):
    return next(iterator)


async def _done() -> None:
    return None


async def _value(value):
    return value


async def _append(values, value) -> None:
    values.append(value)
