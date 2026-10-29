"""Exercise a slow synthetic updater through the real tmux startup watcher."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import sys
import uuid

import libtmux
import pytest

from ccbot.tmux_window import watch_codex_startup_screens


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
async def test_slow_update_relaunches_exact_command_through_real_tmux(tmp_path):
    script = tmp_path / "fake_codex.py"
    marker = tmp_path / "updated"
    invocations = tmp_path / "invocations"
    script.write_text(
        "import pathlib,sys,time\n"
        "marker=pathlib.Path(sys.argv[1]); calls=pathlib.Path(sys.argv[2])\n"
        "with calls.open('a') as f: f.write(repr(sys.argv[1:])+'\\n')\n"
        "if not marker.exists():\n"
        " print('Update available! 1 -> 2\\n› 1. Update now\\n  2. Skip\\nPress enter to continue', flush=True)\n"
        " input(); print('Installing update', flush=True); time.sleep(1.5); marker.touch()\n"
        "else:\n"
        " print('\\x1b[2J\\x1b[HOpenAI Codex\\n\\n› Ask anything\\n\\ngpt-5.6 medium · ~/project', flush=True)\n"
        " while True: time.sleep(1)\n"
    )
    server = libtmux.Server(socket_name=f"ccbot-issue-355-{uuid.uuid4().hex}")
    session = server.new_session(session_name="update-test", x=120, y=30)
    pane = session.active_window.active_pane
    command = " ".join(
        shlex.quote(str(x)) for x in (sys.executable, script, marker, invocations)
    )
    try:
        pane.send_keys(command, enter=True)
        updated = await watch_codex_startup_screens(
            pane,
            command=command,
            timeout=0.8,
            update_timeout=5,
            to_thread=asyncio.to_thread,
            poll_interval=0.05,
            ready_settle_time=0.1,
        )
        assert updated is True
        assert marker.exists()
        lines = invocations.read_text().splitlines()
        assert len(lines) == 2 and lines[0] == lines[1]
    finally:
        server.kill()
