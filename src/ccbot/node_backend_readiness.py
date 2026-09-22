"""Shared backend readiness probes and health payload helpers."""

from __future__ import annotations

import asyncio
import shlex
import shutil
from pathlib import Path
from typing import Any


async def probe_ready_backends(
    configured: tuple[str, ...], *, commands: dict[str, str]
) -> tuple[str, ...]:
    ready: list[str] = []
    for backend in configured:
        try:
            parts = shlex.split(commands.get(backend, backend))
        except ValueError:
            continue
        if not parts:
            continue
        executable = parts[0]
        resolved = (
            executable if Path(executable).is_file() else shutil.which(executable)
        )
        if not resolved:
            continue
        status_args = ["auth", "status"] if backend == "claude" else ["login", "status"]
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                resolved,
                *parts[1:],
                *status_args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return_code = await asyncio.wait_for(process.wait(), timeout=5.0)
        except (TimeoutError, asyncio.TimeoutError):
            if process is not None:
                process.kill()
                await process.wait()
            continue
        except OSError:
            continue
        if return_code == 0:
            ready.append(backend)
    return tuple(ready)


def health_backend_fields(
    configured: tuple[str, ...], ready: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "backends": list(ready),
        "configured_backends": list(configured),
        "ready_backends": list(ready),
        "backend_status": {
            backend: "ready" if backend in ready else "unavailable"
            for backend in configured
        },
    }


def require_ready_backend(value: object, ready: tuple[str, ...]) -> str:
    backend = str(value or "")
    if backend not in ready:
        raise ValueError(
            f"Backend is unavailable on this worker: {backend or 'missing'}"
        )
    return backend


def parse_backend_health(
    payload: dict[str, Any], fallback: list[str]
) -> tuple[list[str], list[str], dict[str, str]]:
    ready = [
        str(value)
        for value in payload.get("backends", [])
        if value in ("claude", "codex")
    ]
    configured_source = payload.get("configured_backends", fallback or ready)
    configured = [
        str(value) for value in configured_source if str(value) in ("claude", "codex")
    ]
    raw_status = payload.get("backend_status", {})
    status = (
        {str(key): str(value) for key, value in raw_status.items()}
        if isinstance(raw_status, dict)
        else {}
    )
    return ready, configured, status


def update_backend_health(node: Any, payload: dict[str, Any]) -> None:
    node.backends, node.configured_backends, node.backend_status = parse_backend_health(
        payload, node.configured_backends
    )
