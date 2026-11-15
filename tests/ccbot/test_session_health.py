"""Process-level liveness, persisted exit notices and safe input regression."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ccbot import session_health as health
from ccbot.handlers import cleanup, message_sender, notifications
from ccbot.session import session_manager
from ccbot.session_models import Session
from ccbot.tmux_manager import TmuxWindow


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(session_manager, "sessions", {})
    monkeypatch.setattr(session_manager, "active_sessions", {})
    monkeypatch.setattr(session_manager, "active_sessions_by_node", {})
    monkeypatch.setattr(session_manager, "active_history", {})
    monkeypatch.setattr(session_manager, "user_settings", {42: {"language": "ru"}})
    monkeypatch.setattr(session_manager, "_resuming_windows", set())
    monkeypatch.setattr(session_manager, "cancel_window_startup", Mock())
    monkeypatch.setattr(session_manager, "save_state", Mock())
    monkeypatch.setattr(health, "_suspected", {})
    monkeypatch.setattr(health, "_expected_stops", set())
    monkeypatch.setattr(health, "_notice_retry", {})
    from ccbot.config import config

    monkeypatch.setattr(config, "allowed_users", {42})
    windows = {"@18": TmuxWindow("@18", "tests-1", "/tmp", "zsh")}
    monkeypatch.setattr(
        health.tmux_manager, "find_window_by_id", AsyncMock(side_effect=windows.get)
    )
    monkeypatch.setattr(health, "pane_has_children", AsyncMock(return_value=False))
    monkeypatch.setattr(
        health.tmux_manager, "kill_window", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(cleanup, "clear_session_state", AsyncMock())
    monkeypatch.setattr(notifications, "refresh_session_keyboard", AsyncMock())
    send = AsyncMock(return_value=SimpleNamespace(message_id=90))
    monkeypatch.setattr(message_sender, "safe_send", send)
    sess = Session(id="dead", name="tests-1", window_id="@18", backend="codex")
    session_manager.sessions[sess.id] = sess
    session_manager.active_sessions[42] = sess.id
    bot = object()

    async def tick(now, panes=None):
        await health.check_session_processes(bot, dict(windows), panes or {}, now=now)

    return SimpleNamespace(sess=sess, windows=windows, send=send, tick=tick, bot=bot)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["codex", "claude", "zcode", "opencode"])
async def test_unchanged_shell_is_lost_and_notified_once_across_restart(env, backend):
    env.sess.backend = backend
    pane = "Error: account/read failed: workspace routing discovery timed out (code -32603)\nsecret-other-data\n#"
    await env.tick(10, {"@18": pane})
    await env.tick(11, {"@18": pane})
    assert env.sess.state == "active"
    env.send.assert_not_awaited()
    await env.tick(12, {"@18": pane})
    assert env.sess.state == "lost"
    assert env.sess.window_id == ""
    assert env.sess.exit_reason == "routing_timeout"
    assert env.sess.exit_notified_users == [42]
    notice = env.send.await_args.args[2]
    assert "tests-1" in notice and "routing discovery timed out" in notice
    assert "secret-other-data" not in notice
    # A fresh process reconstructs acknowledgement from durable session state.
    session_manager.sessions[env.sess.id] = Session.from_dict(env.sess.to_dict())
    health._suspected.clear()
    health._notice_retry.clear()
    await env.tick(100)
    env.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_window_is_confirmed_and_notified(env):
    env.windows.clear()
    await env.tick(10)
    await env.tick(12)
    assert env.sess.state == "lost"
    assert env.sess.exit_reason == "window_missing"
    env.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_transient_snapshot_miss_does_not_kill_live_session(env):
    env.windows["@18"].pane_current_command = "codex"
    await health.check_session_processes(env.bot, {}, {}, now=10)
    await env.tick(12)
    await env.tick(15)
    assert env.sess.state == "active"
    env.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_agent_and_unfamiliar_foreground_process_are_not_crashes(env):
    for command in ("codex", "node", "python3", "opencode", "claude", ""):
        env.windows["@18"].pane_current_command = command
        await env.tick(10)
        await env.tick(1000)
    assert env.sess.state == "active"
    health.tmux_manager.find_window_by_id.assert_not_awaited()
    env.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_exit_is_silent_and_leaves_reserve_for_replacement(env):
    env.sess.mark_default_reserve(42)
    await env.tick(10)
    await env.tick(12)
    assert env.sess.state == "lost"
    assert env.sess.default_reserve_owner == 42
    env.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_intentional_teardown_registers_before_killing_and_is_silent(
    env, monkeypatch
):
    monkeypatch.setattr(
        health.tmux_manager, "kill_window", AsyncMock(return_value=True)
    )
    # Run the real teardown entry point, rather than merely setting a flag.
    await cleanup.teardown_session_runtime(42, env.sess, env.bot)
    await env.tick(10)
    await env.tick(12)
    assert env.sess.state == "active"  # The archive caller owns the transition.
    env.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_grace_is_bounded_and_does_not_hide_failed_start_forever(
    env, monkeypatch
):
    monkeypatch.setattr(health.time, "time", lambda: 1000)
    env.sess.created_at = env.sess.activated_at = 990
    session_manager._resuming_windows.add("@18")
    await env.tick(10)
    await env.tick(12)
    assert env.sess.state == "active"
    monkeypatch.setattr(health.time, "time", lambda: 1300)
    await env.tick(310)
    await env.tick(312)
    assert env.sess.state == "lost"


@pytest.mark.asyncio
async def test_failed_probe_and_foreground_tool_shell_are_not_exit_proof(
    env, monkeypatch
):
    for children in (None, True):
        monkeypatch.setattr(
            health, "pane_has_children", AsyncMock(return_value=children)
        )
        await env.tick(10)
        await env.tick(12)
        assert env.sess.state == "active"
    monkeypatch.setattr(
        health.tmux_manager, "find_window_by_id", AsyncMock(side_effect=OSError)
    )
    await env.tick(20)
    await env.tick(22)
    assert env.sess.state == "active"
    env.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_rebound_window_is_not_marked_lost_by_old_probe(env, monkeypatch):
    async def rebind(_wid):
        env.sess.window_id = "@new"
        return False

    monkeypatch.setattr(health, "window_agent_alive", rebind)
    await env.tick(10)
    await env.tick(12)
    assert env.sess.state == "active" and env.sess.window_id == "@new"
    env.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_notice_failure_is_retried_and_survives_restart(env):
    env.send.return_value = None
    await env.tick(10)
    await env.tick(12)
    assert env.sess.state == "lost" and not env.sess.exit_notified_users
    await env.tick(13)
    assert env.send.await_count == 1
    # The pending obligation survives a bot restart, not just an in-memory retry.
    session_manager.sessions[env.sess.id] = Session.from_dict(env.sess.to_dict())
    health._notice_retry.clear()
    env.send.return_value = SimpleNamespace(message_id=91)
    await env.tick(20)
    assert session_manager.sessions[env.sess.id].exit_notified_users == [42]
    assert env.send.await_count == 2
    await env.tick(100)
    assert env.send.await_count == 2


@pytest.mark.asyncio
async def test_send_refuses_shell_and_keeps_text_out_of_terminal(env, monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(health.tmux_manager, "send_keys", send)
    ok, message = await session_manager.send_to_window("@18", "private prompt")
    assert not ok and "not running" in message
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_session_is_not_checked_against_local_processes(env):
    env.sess.node_id = "worker-a"
    await env.tick(10)
    await env.tick(12)
    assert env.sess.state == "active"
    health.tmux_manager.find_window_by_id.assert_not_awaited()
    env.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows,expected", [("100 1\n", False), ("100 1\n101 100\n", True), ("101 1\n", None)]
)
async def test_process_probe_requires_empty_root_tree(env, monkeypatch, rows, expected):
    read = AsyncMock(side_effect=[(0, "100\n"), (0, rows)])
    monkeypatch.setattr(health, "_read_process_command", read)
    # Restore the real helper hidden by the fixture's conservative process stub.
    assert await _real_pane_has_children("@18") is expected
    assert read.await_args.args == ("ps", "-eo", "pid=,ppid=")


_real_pane_has_children = health.pane_has_children


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_tmux_exit_detection_and_default_replacement(
    env, monkeypatch, tmp_path
):
    """Replay process -> shell using isolated windows and the real PID probe."""
    import asyncio
    import shlex
    import shutil
    import sys
    import uuid

    import libtmux

    from ccbot import default_session
    from ccbot.config import config
    from ccbot.tmux_manager import TmuxManager

    if sys.platform != "linux" or shutil.which("tmux") is None:
        pytest.skip("Linux process names and tmux required")
    name = f"ccbot-health-test-{uuid.uuid4().hex}"
    server = libtmux.Server()
    live = server.new_session(
        session_name=name,
        window_name=config.tmux_main_window_name,
        window_command="/bin/sh",
    )
    manager = TmuxManager(name)
    monkeypatch.setattr(health, "tmux_manager", manager)
    monkeypatch.setattr(health, "pane_has_children", _real_pane_has_children)
    monkeypatch.setattr(default_session, "tmux_manager", manager)
    monkeypatch.setattr(default_session, "_locks", {})
    monkeypatch.setattr(default_session, "_start_retries", {})
    monkeypatch.setattr(session_manager, "mark_window_starting", lambda *_a, **_k: None)
    fake_agent = " ".join(
        shlex.quote(arg)
        for arg in (
            sys.executable,
            "-c",
            "import ctypes,time; ctypes.CDLL(None).prctl(15,b'codex',0,0,0); time.sleep(30)",
        )
    )

    async def wait_command(wid, expected):
        async with asyncio.timeout(8):
            while True:
                window = await manager.find_window_by_id(wid)
                if window and (
                    (
                        expected == "agent"
                        and window.pane_current_command
                        and not health.window_has_exited(window)
                    )
                    or window.pane_current_command == expected
                ):
                    return window
                await asyncio.sleep(0.05)

    async def start_agent():
        window = await asyncio.to_thread(
            live.new_window, window_name="agent", window_shell="/bin/sh"
        )
        await asyncio.to_thread(window.active_pane.send_keys, fake_agent, enter=True)
        await wait_command(window.window_id, "agent")
        return window

    async def real_tick(now):
        windows = {w.window_id: w for w in await manager.polling_snapshot()}
        panes = await manager.capture_panes(list(windows))
        await health.check_session_processes(env.bot, windows, panes, now=now)

    try:
        ordinary = await start_agent()
        env.sess.window_id = ordinary.window_id
        await real_tick(10)
        assert env.sess.state == "active"
        await asyncio.to_thread(ordinary.active_pane.send_keys, "C-c", enter=False)
        await wait_command(ordinary.window_id, "sh")
        await real_tick(11)
        await real_tick(13)
        assert env.sess.state == "lost"
        env.send.assert_awaited_once()

        reserve_window = await start_agent()
        reserve = Session(
            id="reserve",
            name="default",
            window_id=reserve_window.window_id,
            workdir=str(tmp_path),
            backend="codex",
            default_reserve_user_id=42,
        )
        session_manager.sessions[reserve.id] = reserve
        session_manager.user_settings[42].update(
            {
                "default_session_enabled": True,
                "default_session_directory": str(tmp_path),
                "default_session_backend": "codex",
                "enabled_backends": ["codex"],
            }
        )
        await asyncio.to_thread(
            reserve_window.active_pane.send_keys, "C-c", enter=False
        )
        await wait_command(reserve_window.window_id, "sh")

        async def create_window(*_args, **_kwargs):
            window = await start_agent()
            return True, "created", window.window_name, window.window_id

        monkeypatch.setattr(manager, "create_window", create_window)
        replacement = await default_session.ensure_default_session(env.bot, 42)
        assert replacement is not None and replacement.id != reserve.id
        assert replacement.default_reserve_owner == 42
        assert await health.window_agent_alive(replacement.window_id) is True
        assert await manager.find_window_by_id(reserve_window.window_id) is None
        env.send.assert_awaited_once()  # Reserve replacement adds no crash notice.
    finally:
        await manager.close_control_client()
        await asyncio.to_thread(live.kill)


@pytest.mark.asyncio
async def test_crash_does_not_silently_select_a_different_task(env):
    other = Session(id="other-task", name="Other", window_id="@other")
    session_manager.sessions[other.id] = other
    session_manager.active_history[42] = [other.id]
    session_manager.active_sessions_by_node[42] = {"local": env.sess.id}
    await env.tick(10)
    await env.tick(12)
    assert env.sess.state == "lost"
    assert session_manager.get_active_session(42) is None
    assert session_manager.get_session(other.id) is other


async def test_startup_process_exit_preserves_unsubmitted_request_before_rollback(
    env, tmp_path, monkeypatch
):
    from telegram import Update
    from ccbot import input_recovery as recovery_module

    recovery = recovery_module.InputRecovery(tmp_path / "requests.json")
    monkeypatch.setattr(recovery_module, "input_recovery", recovery)
    env.sess.created_at = env.sess.activated_at = 0
    session_manager._resuming_windows.add("@18")
    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "text": "retained prompt",
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
            },
        },
        None,
    )
    request = recovery.admit(update, "text", env.sess)
    await env.tick(10)
    await env.tick(12)
    assert env.sess.state == "lost"
    assert request.startup_deferred
    assert request.state == "failed"
    assert request.id in recovery.records
    assert request.dispatch_started_at == 0
