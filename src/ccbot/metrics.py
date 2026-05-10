"""In-process metrics: counters + a few histograms + JSON-rolling export.

Lightweight observability for a single-host bot — no Prometheus pull
endpoint, no SaaS. Periodically flushed to ``$CCBOT_DIR/metrics.json``
so an external tool (or just `cat`) can scrape it.

Public API:
  inc(name, value=1)     — bump a counter
  observe(name, value)   — record a sample (mean / p95 over the last
                           OBS_WINDOW samples)
  snapshot()             — get a dict of the current state
  flush_to_disk()        — atomic write to metrics.json
  metrics_flush_loop(bot) — background task; runs forever

Conventions for metric names: ``snake_case``, namespaced by feature
prefix (``sessions_*``, ``tg_*``, ``tokens_*``, ``queue_*``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any

from .config import config
from .utils import atomic_write_json

logger = logging.getLogger(__name__)


OBS_WINDOW = 200
FLUSH_INTERVAL_SECONDS = 60.0


_counters: dict[str, int] = defaultdict(int)
_observations: dict[str, deque[float]] = defaultdict(
    lambda: deque(maxlen=OBS_WINDOW)
)
_started_at = time.time()


def inc(name: str, value: int = 1) -> None:
    """Bump counter ``name`` by ``value`` (default 1)."""
    _counters[name] += value


def observe(name: str, value: float) -> None:
    """Record a sample under ``name`` (rolling window of OBS_WINDOW)."""
    _observations[name].append(value)


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    idx = int(round((pct / 100.0) * (len(s) - 1)))
    return s[max(0, min(idx, len(s) - 1))]


def snapshot() -> dict[str, Any]:
    """Return a copy of current counters + observation summaries."""
    obs_summary: dict[str, dict[str, float]] = {}
    for name, samples in _observations.items():
        if not samples:
            continue
        s = list(samples)
        obs_summary[name] = {
            "count": len(s),
            "mean": sum(s) / len(s),
            "min": min(s),
            "max": max(s),
            "p50": _percentile(s, 50),
            "p95": _percentile(s, 95),
        }
    return {
        "uptime_seconds": time.time() - _started_at,
        "counters": dict(_counters),
        "observations": obs_summary,
        "ts": time.time(),
    }


def flush_to_disk() -> None:
    """Atomic snapshot → ``$CCBOT_DIR/metrics.json``."""
    path = config.config_dir / "metrics.json"
    try:
        atomic_write_json(path, snapshot())
    except Exception as e:
        logger.debug("metrics flush failed: %s", e)


async def metrics_flush_loop() -> None:
    """Run forever, flushing the snapshot every ``FLUSH_INTERVAL_SECONDS``."""
    logger.info(
        "Metrics flush loop started (interval %.0fs, file %s/metrics.json)",
        FLUSH_INTERVAL_SECONDS,
        config.config_dir,
    )
    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            flush_to_disk()
        except asyncio.CancelledError:
            logger.info("Metrics flush loop cancelled")
            flush_to_disk()
            raise
        except Exception as e:
            logger.warning("metrics flush iteration failed: %s", e)
