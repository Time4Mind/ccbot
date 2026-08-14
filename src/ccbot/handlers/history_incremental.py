"""Bounded append-only JSONL reader used by live history caching."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..transcript_parser import TranscriptParser
from ..transcript_types import PendingToolInfo

# Keep the transient raw JSON batch small during the initial walk of a large
# transcript.  Retaining every decoded object until EOF multiplies the source
# file size several times over before rendering.
HISTORY_PARSE_BATCH = 256
BOUNDARY_MARKER_SIZE = 64


@dataclass(slots=True)
class IncrementalHistoryState:
    """Append-only parser state for one live transcript.

    Rendered pages live in ``history._pages_cache``; this object deliberately
    does *not* retain the parsed message history. ``pending_tools`` carries only
    unresolved tool-use metadata across append batches so a later tool result
    renders exactly as it does during a one-shot parse. ``observed_*`` describes
    the last file snapshot we attempted; ``offset`` is the last complete JSONL
    line and can lag ``observed_size`` while the writer has an incomplete tail.
    """

    path: str
    device: int
    inode: int
    offset: int = 0
    observed_mtime_ns: int = 0
    observed_size: int = 0
    boundary_marker: bytes = b""
    rendered_total: int = 0
    pending_tools: dict[str, PendingToolInfo] = field(default_factory=dict)


def read_boundary_marker(file_path: Path, offset: int) -> bytes:
    """Return a small fingerprint of the bytes immediately before offset."""
    if offset <= 0:
        return b""
    start = max(0, offset - BOUNDARY_MARKER_SIZE)
    with file_path.open("rb") as transcript:
        transcript.seek(start)
        return transcript.read(offset - start)


def _message_dict(entry: Any) -> dict[str, Any]:
    """Reduce a parsed entry to the fields used by the history renderer."""
    return {
        "role": entry.role,
        "text": entry.text,
        "content_type": entry.content_type,
        "timestamp": entry.timestamp,
    }


def read_history_delta(
    file_path: Path,
    start_offset: int,
    pending_tools: dict[str, PendingToolInfo],
) -> tuple[list[dict[str, Any]], dict[str, PendingToolInfo], int]:
    """Synchronously read and parse complete JSONL lines after an offset.

    The caller runs this under ``asyncio.to_thread``.  Parsing in bounded raw
    batches avoids retaining a second, deeply-decoded copy of a large JSONL.
    The final raw entry of a non-final batch is carried into the next batch so
    a local-command invocation and its immediately-following result cannot be
    split across parser calls (``parse_entries`` tracks that pair locally).
    """

    parsed_messages: list[dict[str, Any]] = []
    pending = dict(pending_tools)
    raw_batch: list[dict[str, Any]] = []
    safe_offset = start_offset

    def consume(*, final: bool) -> None:
        nonlocal pending, raw_batch
        take = len(raw_batch) if final else max(0, len(raw_batch) - 1)
        if take == 0:
            return
        parsed, pending = TranscriptParser.parse_entries(
            raw_batch[:take], pending_tools=pending
        )
        parsed_messages.extend(_message_dict(entry) for entry in parsed)
        raw_batch = raw_batch[take:]

    with file_path.open("rb") as transcript:
        transcript.seek(start_offset)
        while True:
            line = transcript.readline()
            if not line:
                break
            # A writer may be between write() calls.  Never commit the byte
            # offset past a partial line; the completed row is retried after
            # the next size/mtime change.
            if not line.endswith(b"\n"):
                break
            safe_offset = transcript.tell()
            data = TranscriptParser.parse_line(line.decode("utf-8", errors="replace"))
            if data is not None:
                raw_batch.append(data)
            if len(raw_batch) >= HISTORY_PARSE_BATCH + 1:
                consume(final=False)

    consume(final=True)
    return parsed_messages, pending, safe_offset
