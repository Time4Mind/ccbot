"""Wire adapter for extensible worker session-start payloads."""

from __future__ import annotations

from typing import Any, cast


def worker_startup_limits(executor: Any) -> dict[str, Any]:
    get_limits = getattr(executor, "startup_timeouts", None)
    if not callable(get_limits):
        return {"ok": False, "error": "startup limits unsupported"}
    return {"ok": True, **cast(dict[str, Any], get_limits())}


async def dispatch_create_session(
    executor: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
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
    if "startup_limits" in payload:
        kwargs["startup_limits"] = payload["startup_limits"]
    return await executor.create_session(**kwargs)


async def dispatch_context_session(
    executor: Any, pending: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "context_path": str(pending.path),
        "backend": pending.backend,
        "name": pending.name,
    }
    if "startup_limits" in payload:
        kwargs["startup_limits"] = payload["startup_limits"]
    return await executor.start_context_session(**kwargs)
