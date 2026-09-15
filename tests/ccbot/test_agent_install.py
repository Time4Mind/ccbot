from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ccbot import agent_install


@pytest.mark.asyncio
async def test_installer_does_nothing_when_backend_is_already_available(monkeypatch):
    progress = AsyncMock()
    monkeypatch.setattr(agent_install, "is_available", lambda _backend: True)

    assert await agent_install.install("codex", progress)
    progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_npm_is_reported_without_enabling_backend(monkeypatch):
    progress = AsyncMock()
    monkeypatch.setattr(agent_install, "is_available", lambda _backend: False)
    monkeypatch.setattr(agent_install.shutil, "which", lambda _name: None)

    assert not await agent_install.install("claude", progress)
    progress.assert_awaited_once()
    assert "npm" in progress.await_args.args[0]
