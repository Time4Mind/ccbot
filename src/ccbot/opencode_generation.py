"""One-shot OpenCode generation with its own configured provider and session."""

import asyncio
import json
import shlex


async def generate(command: str, prompt: str, *, timeout: float = 90.0) -> str:
    from .config import config

    process = await asyncio.create_subprocess_exec(
        *shlex.split(command),
        "run",
        "--format",
        "json",
        "--model",
        config.opencode_model,
        prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        return ""
    texts = []
    for line in stdout.decode(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") == "text":
            texts.append(str((row.get("part") or {}).get("text") or ""))
    return "\n".join(texts)
