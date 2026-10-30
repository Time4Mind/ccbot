"""Report ZCode's recorded session usage while keeping unknown account quotas unknown."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .zcode_session_io import build_session_file_path, reported_usage


@dataclass(frozen=True)
class ZCodeUsageInfo:
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str = ""


async def fetch_usage() -> ZCodeUsageInfo:
    from .session import session_manager

    def collect() -> ZCodeUsageInfo:
        total = incoming = outgoing = 0
        seen: set[str] = set()
        model = ""
        found = False
        for session in session_manager.sessions.values():
            if (
                session.backend != "zcode"
                or not session.claude_session_id
                or session.claude_session_id in seen
                or session.node_id != "local"
            ):
                continue
            seen.add(session.claude_session_id)
            path = build_session_file_path(session.claude_session_id, session.workdir)
            if path is None:
                continue
            data = reported_usage(path)
            if not data["model"]:
                continue
            found = True
            model = data["model"]
            total += data["total_tokens"]
            incoming += data["input_tokens"]
            outgoing += data["output_tokens"]
        return ZCodeUsageInfo(
            total if found else None,
            incoming if found else None,
            outgoing if found else None,
            model,
        )

    return await asyncio.to_thread(collect)
