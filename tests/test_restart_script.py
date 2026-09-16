"""Public restart.sh behavior for a launchd-managed production bot."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_launchd_restart_waits_for_replacement_pid_without_manual_kill(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    lock = runtime / "ccbot.lock"
    lock.touch()
    pid_file = tmp_path / "pid"
    pid_file.write_text("4100", encoding="utf-8")
    calls = tmp_path / "calls"

    _executable(
        fake_bin / "launchctl",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$FAKE_CALLS"\n'
        'if [ "$1" = print ]; then\n'
        '  printf "state = running\\npid = %s\\n" "$(cat "$FAKE_PID_FILE")"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = kickstart ]; then\n'
        '  printf "4200" > "$FAKE_PID_FILE"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    _executable(
        fake_bin / "lsof",
        '#!/bin/sh\ncat "$FAKE_PID_FILE"\n',
    )
    _executable(
        fake_bin / "kill",
        '#!/bin/sh\nprintf "kill %s\\n" "$*" >> "$FAKE_CALLS"\nexit 99\n',
    )
    _executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")

    repo = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "CCBOT_DIR": str(runtime),
        "CCBOT_LAUNCHD_LABEL": "com.ccbot.test",
        "FAKE_PID_FILE": str(pid_file),
        "FAKE_CALLS": str(calls),
    }
    result = subprocess.run(
        ["bash", str(repo / "scripts/restart.sh")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ccbot restarted by launchd: 4100 -> 4200" in result.stdout
    recorded = calls.read_text(encoding="utf-8")
    assert "kickstart -k" in recorded
    assert "kill " not in recorded
