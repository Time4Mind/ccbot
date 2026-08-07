"""Tmux window creation and agent startup orchestration.

This module builds Claude/Codex commands, creates the libtmux window, and
handles known Codex startup screens. ``TmuxManager`` remains the public API and
passes its patchable dependencies into these helpers explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import Any


def handle_codex_startup_screen(
    pane: object,
    *,
    trust_prompt: str,
    trust_yes: str,
    logger_obj: logging.Logger,
) -> tuple[bool, bool]:
    """Handle one captured startup screen.

    Returns ``(acted, terminal)``. Terminal means the normal Codex input
    is ready or the pane can no longer be inspected.
    """
    capture = getattr(pane, "capture_pane", None)
    send_keys = getattr(pane, "send_keys", None)
    if not callable(capture) or not callable(send_keys):
        return False, True
    try:
        lines = capture()
        text = "\n".join(lines) if isinstance(lines, list) else str(lines)
    except Exception:
        return False, True
    if trust_prompt in text and trust_yes in text:
        send_keys("", enter=True)
        logger_obj.info("Accepted Codex directory trust prompt")
        return True, False
    if (
        "Choose working directory to resume this session" in text
        and "1. Use session directory" in text
        and "2. Use current directory" in text
        and "Press enter to continue" in text
    ):
        send_keys("Down", enter=False)
        send_keys("", enter=True)
        logger_obj.info("Selected current directory for Codex resume")
        return True, False
    if (
        "Update available!" in text
        and "1. Update now" in text
        and "2. Skip" in text
        and "Press enter to continue" in text
    ):
        # Never mutate the host toolchain from a Telegram session.
        send_keys("Down", enter=False)
        send_keys("", enter=True)
        logger_obj.info("Skipped Codex CLI update prompt")
        return True, False
    return False, "OpenAI Codex" in text and "›" in text


def accept_codex_directory_trust(
    pane: object,
    *,
    sleep: Any,
    handler: Any,
) -> bool:
    """Synchronously handle known prompts (small helper/test surface).

    Codex can show this before its normal input box even in full-access
    mode. The bot already owns the selected working directory, so leaving
    the TUI blocked here makes Telegram input appear broken. A resumed
    rollout can also remember a different directory; in that case choose
    the current directory that the user selected for this bot session.
    Poll briefly because the Node wrapper needs a moment to draw prompts.
    """
    accepted = False
    for _ in range(30):
        sleep(0.15)
        acted, terminal = handler(pane)
        accepted = accepted or acted
        if terminal:
            return accepted
    return accepted


async def watch_codex_startup_screens(
    pane: object,
    *,
    timeout: float,
    sleep: Any,
    to_thread: Any,
    handler: Any,
) -> bool:
    """Cancellation-safe long watcher for cold Codex launches."""
    accepted = False
    attempts = max(30, int(max(timeout, 4.5) / 0.15))
    for _ in range(attempts):
        await sleep(0.15)
        acted, terminal = await to_thread(handler, pane)
        accepted = accepted or acted
        if terminal:
            return accepted
    return accepted


async def create_window(
    manager: Any,
    work_dir: str,
    window_name: str | None = None,
    start_claude: bool = True,
    resume_session_id: str | None = None,
    owner_user_id: int | None = None,
    backend: str | None = None,
    initial_prompt: str | None = None,
    *,
    config_obj: Any,
    logger_obj: logging.Logger,
) -> tuple[bool, str, str, str]:
    """Create a new tmux window and optionally start the configured agent.

    Args:
        work_dir: Working directory for the new window
        window_name: Optional window name (defaults to directory name)
        start_claude: Whether to start claude command
        resume_session_id: If set, append --resume <id> to claude command
        owner_user_id: Telegram user_id that created this session, if
            known — exported as ``CCBOT_CHAT_ID`` so ``ccbot send-file``
            (and Claude generally) knows which chat owns this session
            without needing an explicit ``--chat-id``.

    Returns:
        Tuple of (success, message, window_name, window_id)
    """
    # Validate directory first
    path = Path(work_dir).expanduser().resolve()
    selected_backend = backend or config_obj.agent_backend
    if selected_backend not in ("claude", "codex"):
        return False, f"Unsupported agent backend: {selected_backend}", "", ""
    if not path.exists():
        return False, f"Directory does not exist: {work_dir}", "", ""
    if not path.is_dir():
        return False, f"Not a directory: {work_dir}", "", ""

    # Create window name, adding suffix if name already exists
    final_window_name = window_name if window_name else path.name

    # Check for existing window name
    base_name = final_window_name
    counter = 2
    while await manager.find_window_by_name(final_window_name):
        final_window_name = f"{base_name}-{counter}"
        counter += 1

    # Create window in thread
    created_pane: object | None = None

    def _create_and_start() -> tuple[bool, str, str, str]:
        nonlocal created_pane
        session = manager.get_or_create_session()
        try:
            # Create new window
            window = session.new_window(
                window_name=final_window_name,
                start_directory=str(path),
            )

            wid = window.window_id or ""

            # Prevent Claude Code from overriding window name
            window.set_window_option("allow-rename", "off")

            # Start Claude Code if requested
            if start_claude:
                pane = window.active_pane
                if pane:
                    created_pane = pane
                    if selected_backend == "codex":
                        cmd = config_obj.codex_command
                        if config_obj.codex_flags:
                            cmd = f"{cmd} {config_obj.codex_flags}"
                        if resume_session_id:
                            cmd = f"{cmd} resume {shlex.quote(resume_session_id)}"
                    else:
                        cmd = config_obj.claude_command
                        if config_obj.claude_flags:
                            cmd = f"{cmd} {config_obj.claude_flags}"
                        if resume_session_id:
                            cmd = f"{cmd} --resume {shlex.quote(resume_session_id)}"
                    if initial_prompt:
                        cmd = f"{cmd} {shlex.quote(initial_prompt)}"
                    # Identify the runtime so Claude (via the
                    # output-format guidance in CLAUDE.md) can
                    # tailor its replies to the Telegram surface AND
                    # know *which* bot / device hosts the session —
                    # useful when the user runs multiple ccbot
                    # deployments (e.g. Mac + arm64 box).
                    env_prefix = (
                        "CCBOT_INTERFACE=telegram "
                        f"CCBOT_AGENT_BACKEND={selected_backend} "
                        f"CCBOT_DIR={shlex.quote(str(config_obj.config_dir))}"
                    )
                    if config_obj.bot_username:
                        env_prefix += f" CCBOT_BOT_USERNAME={shlex.quote(config_obj.bot_username)}"
                    if config_obj.host_label:
                        env_prefix += (
                            f" CCBOT_HOST={shlex.quote(config_obj.host_label)}"
                        )
                    if owner_user_id is not None:
                        # Lets ``ccbot send-file`` target the right
                        # chat with no argument needed.
                        env_prefix += f" CCBOT_CHAT_ID={owner_user_id}"
                    if config_obj.is_sandbox:
                        cmd = f"IS_SANDBOX=1 {env_prefix} {cmd}"
                    else:
                        cmd = f"{env_prefix} {cmd}"
                    pane.send_keys(cmd, enter=True)

            logger_obj.info(
                "Created window '%s' (id=%s) at %s",
                final_window_name,
                wid,
                path,
            )
            return (
                True,
                f"Created window '{final_window_name}' at {path}",
                final_window_name,
                wid,
            )

        except Exception as e:
            logger_obj.error(f"Failed to create window: {e}")
            return False, f"Failed to create window: {e}", "", ""

    result = await asyncio.to_thread(_create_and_start)
    if result[0] and selected_backend == "codex" and created_pane is not None:
        # Do not hold the Telegram callback open while the Node wrapper
        # draws its startup UI. The background task accepts only the two
        # known directory prompts; normal input is never confirmed.
        task = asyncio.create_task(
            manager._watch_codex_startup_screens(created_pane),
            name=f"codex-startup-trust:{result[3]}",
        )
        manager._startup_tasks.add(task)

        def _finish_startup_task(done: asyncio.Task[bool]) -> None:
            manager._startup_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger_obj.warning("Codex startup prompt handler failed: %s", e)

        task.add_done_callback(_finish_startup_task)
    return result
