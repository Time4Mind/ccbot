"""Match physical Codex input rows without a cross-request scan watermark."""

from __future__ import annotations

from datetime import datetime
import json
import re
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .input_recovery import InputRecovery, InputRequest


_IMAGE_OPEN = re.compile(r'<image name=(\[Image #[1-9]\d*\]) path="([^"\r\n]+)">')


def _matches_content(request: InputRequest, content: list[Any]) -> bool:
    """Compare text literally or decode one complete, request-bound native image."""
    text = "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") in ("input_text", "text")
    )
    images = any(
        isinstance(item, dict) and item.get("type") == "input_image" for item in content
    )
    if request.kind not in ("photo", "document") or not images:
        return text.strip() == request.prepared.strip()
    if not request.media_path:
        return False
    prepared = request.prepared.strip()
    reference = prepared.splitlines()[-1] if prepared else ""
    allowed_paths = {
        request.media_path,
        f".ccbot-inbox/{Path(request.media_path).name}",
    }
    if reference not in allowed_paths:
        return False
    caption = prepared[: -len(reference)].strip()
    remaining = []
    found = False
    index = 0
    while index < len(content):
        item = content[index]
        if not isinstance(item, dict) or item.get("type") not in ("input_text", "text"):
            return False
        part = str(item.get("text") or "")
        opening = _IMAGE_OPEN.fullmatch(part.strip())
        image = content[index + 1] if index + 1 < len(content) else None
        if opening and isinstance(image, dict) and image.get("type") == "input_image":
            if (
                found
                or opening[2] not in allowed_paths
                or not isinstance(image.get("image_url"), str)
                or not image["image_url"]
            ):
                return False
            suffix = content[index + 2 : index + 4]
            if len(suffix) != 2 or any(
                not isinstance(block, dict)
                or block.get("type") not in ("input_text", "text")
                for block in suffix
            ):
                return False
            if (
                str(suffix[0].get("text", "")).strip() != "</image>"
                or str(suffix[1].get("text", "")).strip() != opening[1]
            ):
                return False
            found = True
            index += 4
            continue
        remaining.append(part)
        index += 1
    return found and "\n".join(remaining).strip() == caption


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
                if _matches_content(request, content):
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
