"""Telegram-driven Codex device-code authentication via app-server.

Unlike the Claude flow, the user never pastes a secret back into Telegram.
Codex app-server returns a verification URL plus a short user code and then
emits ``account/login/completed`` after the browser ceremony succeeds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass
from typing import Any

from .config import SENSITIVE_ENV_VARS, config

logger = logging.getLogger(__name__)

FLOW_TTL = 15 * 60.0
_REQUEST_TIMEOUT = 20.0


@dataclass(frozen=True)
class CodexAccountState:
    account_type: str | None
    email: str | None
    plan_type: str | None
    requires_openai_auth: bool

    @property
    def authenticated(self) -> bool:
        return self.account_type is not None or not self.requires_openai_auth


def parse_account_result(result: object) -> CodexAccountState | None:
    if not isinstance(result, dict):
        return None
    account = result.get("account")
    account_type: str | None = None
    email: str | None = None
    plan_type: str | None = None
    if isinstance(account, dict):
        raw_type = account.get("type")
        account_type = str(raw_type) if raw_type else None
        raw_email = account.get("email")
        email = str(raw_email) if raw_email else None
        raw_plan = account.get("planType")
        plan_type = str(raw_plan) if raw_plan else None
    return CodexAccountState(
        account_type=account_type,
        email=email,
        plan_type=plan_type,
        requires_openai_auth=bool(result.get("requiresOpenaiAuth", False)),
    )


class _AppServerConnection:
    def __init__(self, command: str | None = None) -> None:
        self.command = command or config.codex_command
        self.proc: asyncio.subprocess.Process | None = None
        self.stdin: asyncio.StreamWriter | None = None
        self.stdout: asyncio.StreamReader | None = None

    async def start(self) -> None:
        argv = shlex.split(self.command)
        if not argv:
            raise OSError("CODEX_COMMAND is empty")
        env = dict(os.environ)
        env.pop("TMUX", None)
        for name in SENSITIVE_ENV_VARS:
            env.pop(name, None)
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        if self.proc.stdin is None or self.proc.stdout is None:
            await self.close()
            raise OSError("Codex app-server did not expose stdio")
        self.stdin = self.proc.stdin
        self.stdout = self.proc.stdout
        await self.send(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "ccbot",
                        "title": "ccbot",
                        "version": "0.1.0",
                    }
                },
            }
        )
        response = await self.read_response(0, timeout=_REQUEST_TIMEOUT)
        if "error" in response:
            raise OSError(f"Codex app-server initialize failed: {response['error']}")
        await self.send({"method": "initialized", "params": {}})

    async def send(self, message: object) -> None:
        if self.stdin is None:
            raise OSError("Codex app-server stdin is closed")
        self.stdin.write(
            json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        await self.stdin.drain()

    async def read_message(self, timeout: float) -> dict[str, Any]:
        if self.stdout is None:
            raise OSError("Codex app-server stdout is closed")
        while True:
            line = await asyncio.wait_for(self.stdout.readline(), timeout=timeout)
            if not line:
                raise OSError("Codex app-server exited")
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(message, dict):
                return message

    async def read_response(self, request_id: int, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Codex app-server request {request_id} timed out")
            message = await self.read_message(remaining)
            if message.get("id") == request_id:
                return message

    async def request(
        self, method: str, request_id: int, params: object | None = None
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        await self.send(message)
        return await self.read_response(request_id, timeout=_REQUEST_TIMEOUT)

    async def close(self) -> None:
        proc = self.proc
        self.proc = None
        self.stdin = None
        self.stdout = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                return
            await proc.wait()


async def read_account_state(
    *, refresh_token: bool = False, command: str | None = None
) -> CodexAccountState | None:
    """Read Codex auth state without starting or changing a login."""
    conn = _AppServerConnection(command)
    try:
        await conn.start()
        response = await conn.request(
            "account/read", 1, {"refreshToken": refresh_token}
        )
        if "error" in response:
            logger.warning("Codex account/read failed: %s", response["error"])
            return None
        return parse_account_result(response.get("result"))
    except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
        logger.warning("Could not read Codex auth state: %s", exc)
        return None
    finally:
        await conn.close()


class LoginFlow:
    def __init__(self, user_id: int, command: str | None = None) -> None:
        self.user_id = user_id
        self.created_at = time.time()
        self.connection = _AppServerConnection(command)
        self.login_id = ""
        self.verification_url = ""
        self.user_code = ""
        self.cancelled = False

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > FLOW_TTL

    async def start(self) -> bool:
        try:
            await self.connection.start()
            response = await self.connection.request(
                "account/login/start",
                1,
                {"type": "chatgptDeviceCode"},
            )
        except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
            logger.warning("Could not start Codex device login: %s", exc)
            await self.connection.close()
            return False
        result = response.get("result")
        if "error" in response or not isinstance(result, dict):
            logger.warning("Codex login/start failed: %s", response.get("error"))
            await self.connection.close()
            return False
        self.login_id = str(result.get("loginId") or "")
        self.verification_url = str(result.get("verificationUrl") or "")
        self.user_code = str(result.get("userCode") or "")
        if not self.login_id or not self.verification_url or not self.user_code:
            logger.warning(
                "Codex login/start returned an incomplete device-code payload"
            )
            await self.connection.close()
            return False
        return True

    async def wait_completed(self) -> tuple[bool, str]:
        deadline = time.monotonic() + FLOW_TTL
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, "device code expired"
                message = await self.connection.read_message(remaining)
                if message.get("method") != "account/login/completed":
                    continue
                params = message.get("params")
                if not isinstance(params, dict):
                    continue
                if str(params.get("loginId") or "") != self.login_id:
                    continue
                if params.get("success") is True:
                    return True, ""
                return False, str(params.get("error") or "login was not completed")
        except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
            if self.cancelled:
                return False, "cancelled"
            return False, str(exc)
        finally:
            await self.connection.close()

    async def cancel(self) -> None:
        # ``wait_completed`` owns stdout while the flow is live. Closing the
        # dedicated app-server child cancels the pending login without racing
        # a second JSON-RPC reader against that task.
        self.cancelled = True
        await self.connection.close()


_flows: dict[int, LoginFlow] = {}


def get_flow(user_id: int) -> LoginFlow | None:
    flow = _flows.get(user_id)
    if flow is None:
        return None
    if flow.expired:
        _flows.pop(user_id, None)
        asyncio.create_task(flow.cancel())
        return None
    return flow


async def start_flow(user_id: int, *, command: str | None = None) -> LoginFlow | None:
    await cancel_flow(user_id)
    flow = LoginFlow(user_id, command=command)
    if not await flow.start():
        return None
    _flows[user_id] = flow
    return flow


def finish_flow(user_id: int, flow: LoginFlow) -> None:
    if _flows.get(user_id) is flow:
        _flows.pop(user_id, None)


async def cancel_flow(user_id: int) -> None:
    flow = _flows.pop(user_id, None)
    if flow is not None:
        await flow.cancel()


async def cancel_all_flows() -> None:
    flows = list(_flows.values())
    _flows.clear()
    await asyncio.gather(*(flow.cancel() for flow in flows), return_exceptions=True)
