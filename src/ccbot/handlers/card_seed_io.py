"""Bounded reverse-tail reader for live-card transcript restoration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..transcript_codex import normalize_codex_entry
from ..transcript_parser import TranscriptParser
from ..transcript_types import ParsedEntry

_REVERSE_READ_CHUNK = 256 * 1024
_TERMINAL_REASONS = ("end_turn", "stop_sequence", "max_tokens")


def _is_turn_boundary(data: dict[str, Any]) -> bool:
    """Recognise a terminal assistant row without formatting its body."""
    normalized = data
    if data.get("type") not in ("user", "assistant"):
        normalized = normalize_codex_entry(data)
        if normalized is None:
            return False
    if TranscriptParser.get_message_type(normalized) != "assistant":
        return False
    message = normalized.get("message")
    return isinstance(message, dict) and message.get("stop_reason") in _TERMINAL_REASONS


def _recent_raw_entries(file_path: Path, max_turns: int) -> list[dict[str, Any]]:
    """Read backwards until one turn before the requested card history.

    The extra boundary supplies pairing/context immediately before the oldest
    retained answer.  Unlike ``read_text().splitlines()``, old transcript data
    is never materialised or decoded, so a resumed multi-hundred-MB session has
    a bounded startup working set.
    """
    wanted_boundaries = max(1, max_turns) + 1
    reversed_entries: list[dict[str, Any]] = []
    boundaries = 0

    with file_path.open("rb") as transcript:
        transcript.seek(0, 2)
        position = transcript.tell()
        remainder = b""

        while position > 0 and boundaries < wanted_boundaries:
            start = max(0, position - _REVERSE_READ_CHUNK)
            transcript.seek(start)
            block = transcript.read(position - start) + remainder
            position = start
            lines = block.split(b"\n")
            if start > 0:
                remainder = lines[0]
                lines = lines[1:]
            else:
                remainder = b""

            for raw_line in reversed(lines):
                if not raw_line.strip():
                    continue
                data = TranscriptParser.parse_line(
                    raw_line.decode("utf-8", errors="replace")
                )
                if data is None:
                    continue
                reversed_entries.append(data)
                if _is_turn_boundary(data):
                    boundaries += 1
                    if boundaries >= wanted_boundaries:
                        break

    reversed_entries.reverse()
    return reversed_entries


def load_recent_parsed_entries(file_path: Path, max_turns: int) -> list[ParsedEntry]:
    """Load and format only the transcript tail needed by a live card."""
    raw_entries = _recent_raw_entries(file_path, max_turns)
    if not raw_entries:
        return []
    parsed, _pending = TranscriptParser.parse_entries(raw_entries, pending_tools=None)
    return parsed
