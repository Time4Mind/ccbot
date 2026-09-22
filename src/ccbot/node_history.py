"""Bounded worker transcript history seeding over authenticated node RPC."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any, cast

from .handlers.card_seed_io import load_recent_parsed_entries

logger = logging.getLogger(__name__)
MAX_HISTORY_SEED_BYTES = 2 * 1024 * 1024
MAX_HISTORY_SEED_TURNS = 100


class RemoteHistoryMixin:
    async def seed_session_history(
        self,
        target_node_id: str,
        session_id: str,
        max_turns: int,
        *,
        known_version: str = "",
    ) -> dict[str, Any]:
        return await cast(Any, self)._request(
            target_node_id,
            "seed_session_history",
            {
                "session_id": session_id,
                "max_turns": max_turns,
                "known_version": known_version,
            },
        )


def _serialize_entry(entry: Any) -> dict[str, Any]:
    images = []
    for media_type, content in getattr(entry, "image_data", None) or []:
        images.append(
            {
                "media_type": str(media_type),
                "data": base64.b64encode(content).decode("ascii"),
            }
        )
    return {
        "role": str(getattr(entry, "role", "assistant")),
        "text": str(getattr(entry, "text", "") or ""),
        "content_type": str(getattr(entry, "content_type", "text")),
        "tool_use_id": getattr(entry, "tool_use_id", None),
        "timestamp": getattr(entry, "timestamp", None),
        "tool_name": getattr(entry, "tool_name", None),
        "image_data": images,
        "stop_reason": getattr(entry, "stop_reason", None),
        "is_error": bool(getattr(entry, "is_error", False)),
        "api_error": str(getattr(entry, "api_error", "") or ""),
    }


def _bound_entries(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    bounded = list(entries)
    truncated = False
    while (
        bounded
        and len(json.dumps(bounded, ensure_ascii=False).encode())
        > MAX_HISTORY_SEED_BYTES
    ):
        truncated = True
        if len(bounded) > 1:
            bounded.pop(0)
            continue
        bounded[0]["image_data"] = []
        for key in (
            "role",
            "content_type",
            "tool_use_id",
            "timestamp",
            "tool_name",
            "stop_reason",
            "api_error",
        ):
            value = bounded[0].get(key)
            if isinstance(value, str):
                bounded[0][key] = value[-1024:]
        text = str(bounded[0].get("text", ""))[-MAX_HISTORY_SEED_BYTES:]
        bounded[0]["text"] = text
        while (
            len(json.dumps(bounded, ensure_ascii=False).encode())
            > MAX_HISTORY_SEED_BYTES
        ):
            text = text[len(text) // 2 :]
            bounded[0]["text"] = text
        break
    return bounded, truncated


class WorkerHistoryMixin:
    async def seed_session_history(
        self, *, session_id: str, max_turns: int, known_version: str = ""
    ) -> dict[str, Any]:
        session = await cast(Any, self)._find_session(session_id)
        if session is None:
            return {"ok": False, "error": "worker session not found"}
        await cast(Any, self)._bind_transcript(session)
        raw_path = getattr(session, "transcript_path", None)
        path = Path(raw_path) if raw_path else None
        if path is None or not path.is_file():
            return {"ok": True, "version": "", "entries": []}
        stat = path.stat()
        version = f"{stat.st_size}:{stat.st_mtime_ns}"
        if known_version and known_version == version:
            return {"ok": True, "version": version, "unchanged": True, "entries": []}
        turns = min(max(1, int(max_turns)), MAX_HISTORY_SEED_TURNS)
        try:
            parsed = await asyncio.to_thread(load_recent_parsed_entries, path, turns)
            entries, truncated = _bound_entries(
                [_serialize_entry(row) for row in parsed]
            )
        except Exception as exc:
            logger.warning(
                "worker history seed failed session=%s version=%s error=%s",
                session_id,
                version,
                str(exc).strip() or type(exc).__name__,
            )
            return {"ok": False, "error": "worker transcript parse failed"}
        logger.info(
            "worker history seed ready session=%s version=%s entries=%d truncated=%s",
            session_id,
            version,
            len(entries),
            truncated,
        )
        return {
            "ok": True,
            "version": version,
            "entries": entries,
            "truncated": truncated,
        }
