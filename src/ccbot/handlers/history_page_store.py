"""Memory-bounded storage for rendered live-session history pages.

All page numbers remain addressable, but only the two hot edges and a cyclic
window around the page being viewed are retained as ready ``str`` objects.
Cold pages are kept compressed in RAM, so pagination never needs a disk index
and never exposes loading state to Telegram.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import overload
import zlib


class HistoryPageStore(Sequence[str]):
    """Exact-size page collection with a small ready-page working set."""

    def __init__(
        self,
        pages: Iterable[str] = (),
        *,
        edge_pages: int = 5,
        window_radius: int = 10,
    ) -> None:
        self._edge_pages = max(0, edge_pages)
        self._window_radius = max(0, window_radius)
        self._compressed = [self._compress(page) for page in pages]
        self._ready: dict[int, str] = {}
        self._focus: int | None = None
        self._refresh_ready()

    @staticmethod
    def _compress(page: str) -> bytes:
        return zlib.compress(page.encode("utf-8"), level=1)

    def _decompress(self, index: int) -> str:
        return zlib.decompress(self._compressed[index]).decode("utf-8")

    def __len__(self) -> int:
        return len(self._compressed)

    def _normalise(self, index: int) -> int:
        size = len(self)
        if index < 0:
            index += size
        if index < 0 or index >= size:
            raise IndexError("history page index out of range")
        return index

    def _wanted_indices(self) -> set[int]:
        size = len(self)
        if size == 0:
            return set()
        edge = min(self._edge_pages, size)
        wanted = set(range(edge))
        wanted.update(range(size - edge, size))
        if self._focus is not None:
            wanted.update(
                (self._focus + delta) % size
                for delta in range(-self._window_radius, self._window_radius + 1)
            )
        return wanted

    def _refresh_ready(self) -> None:
        wanted = self._wanted_indices()
        self._ready = {
            index: (
                self._ready[index] if index in self._ready else self._decompress(index)
            )
            for index in wanted
        }

    @property
    def ready_indices(self) -> frozenset[int]:
        """Indices currently materialised as ready-to-render strings."""
        return frozenset(self._ready)

    def read(self, index: int, *, focus: bool = True) -> str:
        """Return one page, optionally moving the cyclic ready-page window."""
        index = self._normalise(index)
        if focus:
            self._focus = index
            self._refresh_ready()
        page = self._ready.get(index)
        if page is None:
            page = self._decompress(index)
        return page

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> list[str]: ...

    def __getitem__(self, index: int | slice) -> str | list[str]:
        if isinstance(index, slice):
            return [self._decompress(i) for i in range(*index.indices(len(self)))]
        return self.read(index)

    def __iter__(self) -> Iterator[str]:
        for index in range(len(self)):
            yield self._decompress(index)

    def set_page(self, index: int, page: str) -> None:
        """Replace one page without materialising the rest of the history."""
        index = self._normalise(index)
        self._compressed[index] = self._compress(page)
        if index in self._ready:
            self._ready[index] = page

    def replace_tail(self, count: int, pages: Iterable[str]) -> None:
        """Replace the last ``count`` pages and refresh the bounded hot set."""
        if count < 0 or count > len(self):
            raise ValueError("invalid history tail size")
        if count:
            del self._compressed[-count:]
        self._compressed.extend(self._compress(page) for page in pages)
        if self._focus is not None:
            self._focus = min(self._focus, max(0, len(self) - 1))
        self._ready.clear()
        self._refresh_ready()
