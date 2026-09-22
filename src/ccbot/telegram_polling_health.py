"""Real getUpdates progress tracking and a Docker-safe local health probe."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from telegram.request import BaseRequest

logger = logging.getLogger(__name__)


class PollingHealth:
    """Thread-safe polling progress shared by PTB and the OS watchdog."""

    def __init__(
        self,
        *,
        stale_seconds: float,
        startup_grace_seconds: float,
        state_path: str | Path,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.stale_seconds = stale_seconds
        self.startup_grace_seconds = startup_grace_seconds
        self.state_path = Path(state_path)
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self.started_at = self._clock()
        self.started_wall = self._wall_clock()
        self.last_success_at = 0.0
        self.last_success_wall = 0.0
        self.stopping = False
        self.terminal_claimed = False
        self._write_warning_emitted = False

    def start(self) -> None:
        with self._lock:
            self.started_at = self._clock()
            self.started_wall = self._wall_clock()
            self.last_success_at = 0.0
            self.last_success_wall = 0.0
            self.stopping = False
            self.terminal_claimed = False
            self._publish_locked("starting")

    def record_success(self) -> None:
        with self._lock:
            self.last_success_at = self._clock()
            self.last_success_wall = self._wall_clock()
            self._publish_locked("healthy")

    def mark_stopping(self) -> None:
        with self._lock:
            self.stopping = True
            self._publish_locked("stopping")

    def status(self, *, now: float | None = None) -> tuple[str, float]:
        with self._lock:
            current = self._clock() if now is None else now
            if self.stopping:
                return "stopping", 0.0
            if self.last_success_at:
                age = max(0.0, current - self.last_success_at)
                return ("stale" if age > self.stale_seconds else "healthy"), age
            age = max(0.0, current - self.started_at)
            return (
                "stale" if age > self.startup_grace_seconds else "starting",
                age,
            )

    def claim_terminal(self) -> bool:
        with self._lock:
            if self.stopping or self.terminal_claimed:
                return False
            self.terminal_claimed = True
            return True

    def _publish_locked(self, status: str) -> None:
        payload = {
            "status": status,
            "pid": os.getpid(),
            "started_at": self.started_wall,
            "last_poll_at": self.last_success_wall,
            "stale_after_seconds": self.stale_seconds,
            "startup_grace_seconds": self.startup_grace_seconds,
        }
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            temporary.replace(self.state_path)
            self._write_warning_emitted = False
        except OSError as exc:
            if not self._write_warning_emitted:
                logger.warning("Could not publish polling health state: %s", exc)
                self._write_warning_emitted = True
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class PollingHeartbeatRequest(BaseRequest):
    """Decorate PTB's dedicated getUpdates request with success heartbeats."""

    def __init__(
        self,
        delegate: BaseRequest,
        health: PollingHealth,
        on_success: Callable[[], None] | None = None,
    ) -> None:
        self.delegate = delegate
        self.health = health
        self.on_success = on_success

    @property
    def read_timeout(self) -> float | None:
        return self.delegate.read_timeout

    async def initialize(self) -> None:
        await self.delegate.initialize()

    async def shutdown(self) -> None:
        await self.delegate.shutdown()

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: Any = None,
        read_timeout: Any = BaseRequest.DEFAULT_NONE,
        write_timeout: Any = BaseRequest.DEFAULT_NONE,
        connect_timeout: Any = BaseRequest.DEFAULT_NONE,
        pool_timeout: Any = BaseRequest.DEFAULT_NONE,
    ) -> tuple[int, bytes]:
        result = await self.delegate.do_request(
            url=url,
            method=method,
            request_data=request_data,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            connect_timeout=connect_timeout,
            pool_timeout=pool_timeout,
        )
        try:
            response = json.loads(result[1])
        except (ValueError, TypeError):
            response = {}
        if not isinstance(response, dict):
            response = {}
        if 200 <= result[0] < 300 and response.get("ok") is True:
            self.health.record_success()
            if self.on_success is not None:
                self.on_success()
        return result


def probe_health_file(path: str | Path, *, now: float | None = None) -> bool:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        os.kill(int(payload["pid"]), 0)
        current = time.time() if now is None else now
        status = str(payload.get("status", ""))
        if status == "starting":
            return current - float(payload["started_at"]) <= float(
                payload["startup_grace_seconds"]
            )
        if status != "healthy":
            return False
        return current - float(payload["last_poll_at"]) <= float(
            payload["stale_after_seconds"]
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def main() -> None:
    from .utils import ccbot_dir

    path = os.environ.get(
        "CCBOT_POLL_HEALTH_FILE", str(ccbot_dir() / "poll-health.json")
    )
    raise SystemExit(0 if probe_health_file(path) else 1)
