"""Match physical Codex input rows without a cross-request scan watermark."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .input_recovery import InputRecovery, InputRequest


def reconcile_codex(
    recovery: InputRecovery, request: InputRequest, scan_positions: dict[str, int]
) -> bool:
    assert request.checkpoint is not None
    path = Path(request.checkpoint["path"])
    offset = int(request.checkpoint["offset"])
    # One physical user row can acknowledge at most one request,
    # even when two identical prompts were sent before either appeared.
    base_path_key = str(path)
    device = request.checkpoint.get("device")
    inode = request.checkpoint.get("inode")
    path_key = (
        f"{base_path_key}#{device}:{inode}"
        if device is not None and inode is not None
        else base_path_key
    )
    # Legacy readers retain their watermark for rollback. New readers track
    # individual claimed rows: a later confirmation cannot hide an earlier one.
    if path_key not in recovery.confirmed_rows:
        legacy = (
            0
            if request.checkpoint.get("replaced")
            else recovery.confirmed_through.get(base_path_key, 0)
        )
        recovery.confirmed_rows[path_key] = [legacy] if legacy else []
    claimed = recovery.confirmed_rows[path_key]
    start = max(offset, scan_positions.get(request.id, 0))
    try:
        if path.stat().st_size < start:
            return False
        with path.open("rb") as stream:
            stream.seek(start)
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                scan_positions[request.id] = stream.tell()
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(row, dict) or row.get("type") != "response_item":
                    continue
                payload = row.get("payload")
                if (
                    not isinstance(payload, dict)
                    or payload.get("type") != "message"
                    or payload.get("role") != "user"
                ):
                    continue
                not_before = float(request.checkpoint.get("not_before", 0))
                if not_before:
                    try:
                        stamp = datetime.fromisoformat(
                            str(row.get("timestamp", "")).replace("Z", "+00:00")
                        ).timestamp()
                    except (TypeError, ValueError, OSError):
                        continue
                    if stamp < not_before:
                        continue
                content = payload.get("content", [])
                if not isinstance(content, list):
                    continue
                text = "\n".join(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict)
                    and item.get("type") in ("input_text", "text")
                )
                if text.strip() == request.prepared.strip():
                    if stream.tell() in claimed:
                        continue
                    claimed.append(stream.tell())
                    recovery.confirmed_through[path_key] = max(
                        stream.tell(), recovery.confirmed_through.get(path_key, 0)
                    )
                    recovery.confirmed_through[base_path_key] = max(
                        stream.tell(), recovery.confirmed_through.get(base_path_key, 0)
                    )
                    if len(recovery.confirmed_through) > 512:
                        recovery.confirmed_through.pop(
                            next(iter(recovery.confirmed_through))
                        )
                    recovery.complete(request)
                    return True
    except OSError:
        return False
    return False
