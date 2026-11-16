"""Read OpenCode's available models from the session's native configuration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import shlex

from .config import config
from .session_models import Session


@dataclass(frozen=True)
class ModelChoice:
    provider: str
    id: str
    name: str
    variants: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.name} · {self.provider}"


def parse_catalogue(output: str) -> tuple[ModelChoice, ...]:
    """The native verbose stream alternates provider/model IDs and JSON objects."""
    decoder = json.JSONDecoder()
    pos = 0
    result: dict[tuple[str, str], ModelChoice] = {}
    while pos < len(output):
        line, _, _ = output[pos:].partition("\n")
        pos += len(line) + 1
        if "/" not in line or pos >= len(output):
            continue
        try:
            item, end = decoder.raw_decode(output, pos)
        except ValueError:
            continue
        pos = end
        if not isinstance(item, dict):
            continue
        provider, model_id = item.get("providerID"), item.get("id")
        if not isinstance(provider, str) or not isinstance(model_id, str):
            continue
        if (
            line.strip() != f"{provider}/{model_id}"
            or item.get("status") == "deprecated"
        ):
            continue
        if provider == "opencode" and "-nano" in model_id:
            continue
        name = item.get("name") or model_id
        if not isinstance(name, str) or any(ord(c) < 32 for c in name):
            continue
        variants = item.get("variants", {})
        modes = (
            tuple(
                k
                for k, v in variants.items()
                if isinstance(k, str)
                and not (isinstance(v, dict) and v.get("disabled"))
            )
            if isinstance(variants, dict)
            else ()
        )
        result[provider, model_id] = ModelChoice(provider, model_id, name, modes)
    if not result:
        raise ValueError("OpenCode returned no available models")
    return tuple(
        sorted(
            result.values(),
            key=lambda m: (m.provider != "openai", m.provider, m.name.lower(), m.id),
        )
    )


async def load_catalogue(sess: Session) -> tuple[ModelChoice, ...]:
    # A leader catalogue must never stand in for a different node's configuration.
    if sess.node_id != "local":
        raise ValueError("OpenCode model catalogue requires this node's native CLI")
    process = await asyncio.create_subprocess_exec(
        *shlex.split(config.opencode_command),
        "models",
        "--verbose",
        cwd=sess.workdir,
        env={**os.environ, "CCBOT_AGENT_BACKEND": "opencode"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise ValueError("OpenCode model catalogue is unavailable")
    if len(output) > 16 * 1024 * 1024:
        raise ValueError("OpenCode model catalogue is too large")
    return parse_catalogue(output.decode("utf-8"))
