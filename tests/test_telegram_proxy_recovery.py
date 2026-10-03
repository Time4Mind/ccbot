"""Offline proxy failures through the actual application Bot API requests."""

from __future__ import annotations

import asyncio
import json

import httpcore
import pytest
from telegram.error import TimedOut

from ccbot import rich
from ccbot.bot.app import create_bot
from ccbot.bot import app
from ccbot.config import config
from ccbot.telegram_polling_health import PollingHealth, probe_health_file


class FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, backend, mode):
        self.backend = backend
        self.mode = mode
        self.closed = False
        self.tls_complete = False

    async def read(self, max_bytes, timeout=None):
        if not self.tls_complete:
            if self.mode == "connect_cancel":
                await asyncio.Event().wait()
            return b"HTTP/1.1 200 Connection Established\r\n\r\n"
        if self.mode == "active":
            self.backend.active_started.set()
            await self.backend.active_release.wait()
        body = json.dumps(
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": 1, "type": "private"},
                    "text": "reply",
                },
            }
        ).encode()
        return (
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )

    async def write(self, buffer, timeout=None):
        pass

    async def aclose(self):
        if self.backend.slow_close:
            self.backend.close_started.set()
            await self.backend.close_release.wait()
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        if self.mode == "tls_timeout":
            raise httpcore.ConnectTimeout("offline TLS failure")
        if self.mode == "tls_cancel":
            self.backend.tls_started.set()
            await asyncio.Event().wait()
        self.tls_complete = True
        return self

    def get_extra_info(self, info):
        return False if info == "is_readable" else None


class FakeBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, mode):
        self.mode = mode
        self.streams = []
        self.active_started = asyncio.Event()
        self.active_release = asyncio.Event()
        self.tls_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.slow_close = False

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        stream = FakeStream(self, self.mode)
        self.streams.append(stream)
        return stream


def application_with_backend(monkeypatch, mode):
    monkeypatch.setattr(config, "tg_proxy_url", "http://proxy.invalid:1081")
    application = create_bot()
    request = application.bot.request
    delegate = getattr(request, "delegate", request)
    backend = FakeBackend(mode)
    delegate._client._transport._pool._network_backend = backend
    return application, backend


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tls_cancel", "tls_timeout", "connect_cancel"])
async def test_failed_proxy_connections_do_not_block_replies_after_network_recovers(
    monkeypatch, mode
):
    application, backend = application_with_backend(monkeypatch, mode)
    try:
        for _ in range(16):
            with pytest.raises((TimeoutError, TimedOut)):
                await asyncio.wait_for(
                    application.bot.send_message(1, "reply", pool_timeout=0.01), 0.01
                )
        backend.mode = "healthy"
        reply = await application.bot.send_message(1, "recovered", pool_timeout=0.01)
        assert reply.text == "reply"
    finally:
        await application.bot.request.shutdown()
        await application.bot._request[0].shutdown()


@pytest.mark.asyncio
async def test_actual_rich_edit_deadlines_do_not_exhaust_outbound_pool(monkeypatch):
    application, backend = application_with_backend(monkeypatch, "tls_cancel")
    try:
        for _ in range(16):
            with pytest.raises(TimeoutError):
                # Keep the production .75s deadline: integration must exercise
                # the exact cancellation seam that triggered the incident.
                await rich.edit_rich_message(application.bot, 1, 1, "card")
        backend.mode = "healthy"
        reply = await application.bot.send_message(1, "recovered", pool_timeout=0.01)
        assert reply.text == "reply"
        edited = await rich.edit_rich_message(application.bot, 1, 1, "card recovered")
        assert edited is not None and edited.message_id == 1
    finally:
        await application.bot.request.shutdown()
        await application.bot._request[0].shutdown()


@pytest.mark.asyncio
async def test_failed_tls_cleanup_does_not_close_concurrent_active_reply(monkeypatch):
    application, backend = application_with_backend(monkeypatch, "active")
    active = asyncio.create_task(application.bot.send_message(1, "active reply"))
    try:
        await asyncio.wait_for(backend.active_started.wait(), 1)
        backend.mode = "tls_timeout"
        for _ in range(16):
            with pytest.raises(TimedOut):
                await application.bot.send_message(1, "failed", pool_timeout=0.01)
        assert not backend.streams[0].closed
        backend.mode = "healthy"
        reply = await application.bot.send_message(1, "recovered", pool_timeout=0.01)
        assert reply.text == "reply"
        backend.active_release.set()
        assert (await active).text == "reply"
    finally:
        backend.active_release.set()
        await asyncio.gather(active, return_exceptions=True)
        await application.bot.request.shutdown()
        await application.bot._request[0].shutdown()


@pytest.mark.asyncio
async def test_second_cancellation_waits_for_failed_tunnel_cleanup(monkeypatch):
    application, backend = application_with_backend(monkeypatch, "tls_cancel")
    backend.slow_close = True
    request = asyncio.create_task(application.bot.send_message(1, "cancelled"))
    try:
        await asyncio.wait_for(backend.tls_started.wait(), 1)
        request.cancel()
        await asyncio.wait_for(backend.close_started.wait(), 1)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        backend.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        backend.mode = "healthy"
        backend.slow_close = False
        assert (
            await application.bot.send_message(1, "recovered", pool_timeout=0.01)
        ).text == "reply"
    finally:
        backend.close_release.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await application.bot.request.shutdown()
        await application.bot._request[0].shutdown()


@pytest.mark.asyncio
async def test_polling_success_cannot_hide_sustained_outbound_failure(
    monkeypatch, tmp_path
):
    now = [1000.0]
    health = PollingHealth(
        stale_seconds=180,
        startup_grace_seconds=120,
        state_path=tmp_path / "health.json",
        clock=lambda: now[0],
        wall_clock=lambda: now[0],
    )
    health.start()
    monkeypatch.setattr(app, "polling_health", health)
    monkeypatch.setattr(app.time, "monotonic", lambda: now[0])
    terminate = []
    monkeypatch.setattr(
        app, "_terminate_for_sustained_conflict", lambda: terminate.append(True)
    )
    application, backend = application_with_backend(monkeypatch, "tls_timeout")
    polling = application.bot._request[0]
    polling.delegate._client._transport._pool._network_backend = FakeBackend("healthy")
    try:
        for tick in range(7):
            now[0] = 1000.0 + tick * 40
            app._last_heartbeat = now[0]
            with pytest.raises(TimedOut):
                await application.bot.send_message(1, "failed")
            await polling.do_request(
                url="https://telegram.invalid/getUpdates", method="POST"
            )
        assert health.status()[0] == "healthy"  # Incoming polling really works.
        assert not probe_health_file(health.state_path, now=now[0])
        app._liveness_watchdog_tick()
        assert terminate == [True]
        backend.mode = "healthy"
        assert (await application.bot.send_message(1, "recovered")).text == "reply"
        assert probe_health_file(health.state_path, now=now[0])
    finally:
        await application.bot.request.shutdown()
        await polling.shutdown()


@pytest.mark.asyncio
async def test_one_failed_send_does_not_restart_an_idle_bot(monkeypatch, tmp_path):
    now = [1000.0]
    health = PollingHealth(
        stale_seconds=180,
        startup_grace_seconds=120,
        state_path=tmp_path / "health.json",
        clock=lambda: now[0],
        wall_clock=lambda: now[0],
    )
    health.start()
    monkeypatch.setattr(app, "polling_health", health)
    monkeypatch.setattr(app.time, "monotonic", lambda: now[0])
    terminate = []
    monkeypatch.setattr(
        app, "_terminate_for_sustained_conflict", lambda: terminate.append(True)
    )
    application, _backend = application_with_backend(monkeypatch, "tls_timeout")
    try:
        with pytest.raises(TimedOut):
            await application.bot.send_message(1, "failed")
        now[0] += 500
        app._last_heartbeat = now[0]
        health.record_success()
        assert probe_health_file(health.state_path, now=now[0])
        app._liveness_watchdog_tick()
        assert not terminate
        with pytest.raises(TimedOut):
            await application.bot.send_message(1, "new isolated failure")
        assert probe_health_file(health.state_path, now=now[0])
    finally:
        await application.bot.request.shutdown()
        await application.bot._request[0].shutdown()
