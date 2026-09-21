"""Wire adapter for extensible worker session-start payloads."""

from __future__ import annotations

from typing import Any


async def dispatch_create_session(
    executor: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    kwargs = {
        "path": str(payload.get("path", "")),
        "backend": str(payload.get("backend", "")),
        "name": str(payload.get("name", "")),
        "startup_id": str(payload.get("startup_id", "")),
    }
    kwargs.update(
        {
            key: str(payload[key])
            for key in (
                "resume_session_id",
                "source_backend",
                "provider_transcript_path",
            )
            if payload.get(key)
        }
    )
    return await executor.create_session(**kwargs)
