"""Enforce the repository's per-module line-count budgets.

Telegram application modules under ``src/ccbot/bot`` are capped at 600
physical lines; every other Python source module is capped at 800. The check
is intentionally simple and deterministic so it behaves the same locally and
in CI.
"""

from __future__ import annotations

import argparse
from pathlib import Path

BOT_LIMIT = 600
DEFAULT_LIMIT = 800


def module_limit(path: Path, source_root: Path) -> int:
    """Return the applicable hard limit for one source module."""
    relative = path.relative_to(source_root)
    return BOT_LIMIT if relative.parts[0] == "bot" else DEFAULT_LIMIT


def oversized_modules(source_root: Path) -> list[tuple[Path, int, int]]:
    """Collect ``(path, actual_lines, limit)`` for every violation."""
    violations: list[tuple[Path, int, int]] = []
    for path in sorted(source_root.rglob("*.py")):
        actual = len(path.read_text(encoding="utf-8").splitlines())
        limit = module_limit(path, source_root)
        if actual > limit:
            violations.append((path, actual, limit))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_root",
        nargs="?",
        type=Path,
        default=Path("src/ccbot"),
    )
    args = parser.parse_args()
    violations = oversized_modules(args.source_root)
    for path, actual, limit in violations:
        print(f"{path}: {actual} lines exceeds {limit}")
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main())
