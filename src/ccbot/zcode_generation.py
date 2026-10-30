"""Run isolated, tool-free ZCode requests for naming and connection checks."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import signal
import tempfile

_TOOLS = "Agent,AskUserQuestion,Bash,Edit,EnterPlanMode,ExitPlanMode,Read,SendMessage,Skill,TaskOutput,TaskStop,TodoRead,TodoWrite,ToolSearch,WebFetch,WebSearch,Write,DeferredToolCall,ReadSessionContext"


async def generate(command: str, prompt: str, *, timeout: float = 30.0) -> str | None:
    args = shlex.split(command)
    if not args:
        return None
    with tempfile.TemporaryDirectory(prefix="ccbot-zcode-generation-") as directory:
        path = Path(directory)
        (path / "zcode.json").write_text(
            json.dumps(
                {
                    "features": {
                        "mcp": False,
                        "subagent": False,
                        "memory": False,
                        "skill": False,
                    },
                    "plugins": {"enabled": False},
                    "hooks": {"enabled": False},
                    "memory": {"use": False},
                    "skills": {"enabled": False},
                }
            )
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("CCBOT_")
            and k not in {"TMUX", "TMUX_PANE", "TMUX_TMPDIR"}
        }
        env.update(
            ZCODE_SESSION_DB_PATH=str(path / "db.sqlite"),
            ZCODE_STORAGE_DIR=str(path / "storage"),
            ZCODE_LOG_DIR=str(path / "logs"),
            ZCODE_MODEL_TELEMETRY_ENABLED="0",
        )
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                "--prompt",
                prompt,
                "--mode",
                "plan",
                "--disallowed-tools",
                _TOOLS,
                "--json",
                "--no-color",
                "--cwd",
                directory,
                cwd=directory,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode != 0:
                return None
            data = json.loads(output)
            if not isinstance(data, dict):
                return None
            response = data.get("response")
            return response if isinstance(response, str) else None
        except (OSError, ValueError, TimeoutError):
            return None
        finally:
            if process is not None and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                except ProcessLookupError:
                    await process.wait()
