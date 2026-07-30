"""Codex device auth without touching the real CLI or credential store."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ccbot import codex_auth


def test_account_state_requires_login() -> None:
    state = codex_auth.parse_account_result(
        {"account": None, "requiresOpenaiAuth": True}
    )
    assert state is not None
    assert state.authenticated is False


def test_account_state_accepts_chatgpt_login() -> None:
    state = codex_auth.parse_account_result(
        {
            "account": {
                "type": "chatgpt",
                "email": "person@example.com",
                "planType": "pro",
            },
            "requiresOpenaiAuth": True,
        }
    )
    assert state is not None
    assert state.authenticated is True
    assert state.plan_type == "pro"


class _FakeConnection:
    def __init__(self, _command: str | None = None) -> None:
        self.proc = object()
        self.closed = False
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def start(self) -> None:
        return None

    async def request(
        self, method: str, request_id: int, params: object | None = None
    ) -> dict[str, Any]:
        assert method == "account/login/start"
        assert request_id == 1
        assert params == {"type": "chatgptDeviceCode"}
        return {
            "id": 1,
            "result": {
                "type": "chatgptDeviceCode",
                "loginId": "login-1",
                "verificationUrl": "https://auth.openai.com/codex/device",
                "userCode": "ABCD-1234",
            },
        }

    async def read_message(self, _timeout: float) -> dict[str, Any]:
        message = await self.messages.get()
        if message.get("__closed__"):
            raise OSError("closed")
        return message

    async def close(self) -> None:
        self.closed = True
        self.messages.put_nowait({"__closed__": True})


@pytest.mark.asyncio
async def test_device_flow_waits_for_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_auth, "_AppServerConnection", _FakeConnection)
    flow = codex_auth.LoginFlow(42, command="fake-codex")

    assert await flow.start() is True
    assert flow.user_code == "ABCD-1234"
    await flow.connection.messages.put(  # type: ignore[attr-defined]
        {
            "method": "account/login/completed",
            "params": {
                "loginId": "login-1",
                "success": True,
                "error": None,
            },
        }
    )

    assert await flow.wait_completed() == (True, "")
    assert flow.connection.closed is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cancelled_device_flow_is_not_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_auth, "_AppServerConnection", _FakeConnection)
    flow = codex_auth.LoginFlow(42, command="fake-codex")
    assert await flow.start() is True

    waiter = asyncio.create_task(flow.wait_completed())
    await asyncio.sleep(0)
    await flow.cancel()
    assert await waiter == (False, "cancelled")


@pytest.mark.asyncio
async def test_startup_preflight_starts_device_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ccbot.bot.commands import auth as auth_cmd

    monkeypatch.setattr(auth_cmd.session_manager, "agent_backend", "codex")
    monkeypatch.setattr(auth_cmd.config, "allowed_users", {7})

    async def fake_state(**_kwargs: object) -> codex_auth.CodexAccountState:
        return codex_auth.CodexAccountState(None, None, None, True)

    started: list[int] = []

    async def fake_start(_bot: object, user_id: int) -> bool:
        started.append(user_id)
        return True

    monkeypatch.setattr(auth_cmd.codex_auth, "read_account_state", fake_state)
    monkeypatch.setattr(auth_cmd, "start_login", fake_start)

    await auth_cmd.ensure_codex_auth_on_start(object())

    assert started == [7]


@pytest.mark.asyncio
async def test_startup_preflight_is_silent_when_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ccbot.bot.commands import auth as auth_cmd

    monkeypatch.setattr(auth_cmd.session_manager, "agent_backend", "codex")

    async def fake_state(**_kwargs: object) -> codex_auth.CodexAccountState:
        return codex_auth.CodexAccountState("chatgpt", None, "plus", True)

    async def must_not_start(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("login flow started for an authenticated account")

    monkeypatch.setattr(auth_cmd.codex_auth, "read_account_state", fake_state)
    monkeypatch.setattr(auth_cmd, "start_login", must_not_start)

    await auth_cmd.ensure_codex_auth_on_start(object())


@pytest.mark.asyncio
async def test_login_command_posts_device_code_and_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ccbot.bot.commands import auth as auth_cmd

    monkeypatch.setattr(auth_cmd.session_manager, "agent_backend", "codex")
    sent: list[str] = []
    restored: list[int] = []

    class FakeFlow:
        verification_url = "https://auth.openai.com/codex/device"
        user_code = "WXYZ-9876"

        async def wait_completed(self) -> tuple[bool, str]:
            return True, ""

    flow = FakeFlow()

    async def fake_start_flow(*_args: object, **_kwargs: object) -> FakeFlow:
        return flow

    async def fake_send(
        _bot: object, _chat_id: int, text: str, **_kwargs: object
    ) -> None:
        sent.append(text)

    async def fake_restore(_bot: object, user_id: int) -> None:
        restored.append(user_id)

    monkeypatch.setattr(auth_cmd.codex_auth, "start_flow", fake_start_flow)
    monkeypatch.setattr(auth_cmd.codex_auth, "finish_flow", lambda *_args: None)
    monkeypatch.setattr(auth_cmd, "safe_send", fake_send)
    monkeypatch.setattr(auth_cmd, "_restore_working_surface", fake_restore)

    assert await auth_cmd.start_login(object(), 9) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert any("WXYZ-9876" in text for text in sent)
    assert any("authorized" in text.lower() for text in sent)
    assert restored == [9]
