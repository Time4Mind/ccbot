"""Read Codex account rate limits through the supported app-server API."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from dataclasses import dataclass
from typing import Any

from .config import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexRateLimitWindow:
    used_percent: int
    duration_minutes: int
    resets_at: int | None


@dataclass(frozen=True)
class CodexUsageInfo:
    five_hour: CodexRateLimitWindow | None = None
    weekly: CodexRateLimitWindow | None = None


def parse_rate_limits_result(result: object) -> CodexUsageInfo | None:
    """Normalize app-server ``account/rateLimits/read`` into known windows."""
    if not isinstance(result, dict):
        return None

    rate_limits = result.get("rateLimits")
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        codex_bucket = by_id.get("codex")
        if isinstance(codex_bucket, dict):
            rate_limits = codex_bucket
    if not isinstance(rate_limits, dict):
        return None

    windows: list[CodexRateLimitWindow] = []
    for key in ("primary", "secondary"):
        raw = rate_limits.get(key)
        if not isinstance(raw, dict):
            continue
        try:
            used = max(0, min(100, int(round(float(raw["usedPercent"])))))
            duration = int(raw["windowDurationMins"])
        except (KeyError, TypeError, ValueError):
            continue
        reset_raw = raw.get("resetsAt")
        try:
            resets_at = int(reset_raw) if reset_raw is not None else None
        except (TypeError, ValueError):
            resets_at = None
        windows.append(
            CodexRateLimitWindow(
                used_percent=used,
                duration_minutes=duration,
                resets_at=resets_at,
            )
        )

    if not windows:
        return None

    five_hour = next((w for w in windows if w.duration_minutes == 5 * 60), None)
    weekly = next((w for w in windows if w.duration_minutes == 7 * 24 * 60), None)

    # Keep working if the service changes the exact interval slightly:
    # short windows belong to the session bucket, multi-day windows to week.
    if five_hour is None:
        five_hour = next((w for w in windows if w.duration_minutes < 24 * 60), None)
    if weekly is None:
        weekly = next((w for w in windows if w.duration_minutes >= 24 * 60), None)

    return CodexUsageInfo(five_hour=five_hour, weekly=weekly)


async def fetch_codex_usage(timeout: float = 12.0) -> CodexUsageInfo | None:
    """Fetch limits without opening or sending keys to any Codex TUI session."""
    command = shlex.split(config.codex_command)
    if not command:
        return None

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdin is None or proc.stdout is None:
            return None
        stdin = proc.stdin
        stdout = proc.stdout

        messages = (
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "ccbot",
                        "title": "ccbot",
                        "version": "0.1.0",
                    }
                },
            },
            {"method": "initialized", "params": {}},
            {"method": "account/rateLimits/read", "id": 1},
        )
        for message in messages:
            stdin.write(
                json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
            )
        await stdin.drain()

        async def _read_result() -> CodexUsageInfo | None:
            while line := await stdout.readline():
                try:
                    response: Any = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if response.get("id") != 1:
                    continue
                if "error" in response:
                    logger.debug("Codex rate-limits error: %s", response["error"])
                    return None
                return parse_rate_limits_result(response.get("result"))
            return None

        return await asyncio.wait_for(_read_result(), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as e:
        logger.debug("Codex usage fetch failed: %s", e)
        return None
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
