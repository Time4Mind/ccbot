from __future__ import annotations

from ccbot.handlers.history_page_store import HistoryPageStore


def _pages(count: int) -> list[str]:
    return [f"page-{index}-" + (str(index) * 200) for index in range(count)]


def test_hot_start_exposes_exact_total_and_materializes_both_edges() -> None:
    store = HistoryPageStore(_pages(100))

    assert len(store) == 100
    assert store.ready_indices == frozenset({0, 1, 2, 3, 4, 95, 96, 97, 98, 99})


def test_middle_page_keeps_pinned_edges_and_plus_minus_ten_window() -> None:
    store = HistoryPageStore(_pages(100))

    assert store[50].startswith("page-50-")

    assert store.ready_indices == frozenset(
        {*range(0, 5), *range(40, 61), *range(95, 100)}
    )
    assert len(store.ready_indices) == 31


def test_window_wraps_cyclically_at_the_last_page() -> None:
    store = HistoryPageStore(_pages(100))

    assert store[99].startswith("page-99-")

    assert store.ready_indices == frozenset({*range(0, 10), *range(89, 100)})


def test_replacing_tail_preserves_total_and_hot_last_pages() -> None:
    store = HistoryPageStore(_pages(30))

    store.replace_tail(1, ["replacement-a", "replacement-b"])

    assert len(store) == 31
    assert store[-2] == "replacement-a"
    assert store[-1] == "replacement-b"
    assert set(range(26, 31)).issubset(store.ready_indices)
