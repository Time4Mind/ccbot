"""Read JSONL backwards without repeatedly copying a multi-chunk record."""

from collections.abc import Iterator
from typing import BinaryIO


def reverse_lines(stream: BinaryIO, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
    position = stream.seek(0, 2)
    fragments: list[bytes] = []
    while position:
        size = min(position, chunk_size)
        position -= size
        stream.seek(position)
        pieces = stream.read(size).split(b"\n")
        if len(pieces) > 1:
            fragments.append(pieces.pop())
            yield b"".join(reversed(fragments))
            fragments.clear()
            yield from reversed(pieces[1:])
        fragments.append(pieces[0])
    if fragments:
        yield b"".join(reversed(fragments))
