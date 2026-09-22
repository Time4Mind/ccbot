from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.node_models import Node
from ccbot.node_notifications import NodeNotificationMonitor


@pytest.mark.asyncio
async def test_node_disconnect_notifications_are_opt_in_and_deduplicated():
    node = Node(
        id="worker-a",
        display_name="Worker A",
        state="ready",
        last_seen_at=100.0,
    )
    enabled = False
    manager = SimpleNamespace(
        list_nodes=lambda: [Node.local(), node],
        get_user_settings=lambda _uid: {"bg_notify_node_status": enabled},
        get_selected_node_id=lambda _uid: "worker-a",
        active_sessions_by_node={},
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    monitor = NodeNotificationMonitor(manager=manager, now=lambda: 161.0)

    await monitor.poll_once(bot, [42])
    bot.send_message.assert_not_awaited()

    enabled = True
    await monitor.poll_once(bot, [42])
    assert bot.send_message.await_count == 1
    assert "Worker A" in bot.send_message.await_args.kwargs["text"]

    await monitor.poll_once(bot, [42])
    assert bot.send_message.await_count == 1

    node.last_seen_at = 160.0
    await monitor.poll_once(bot, [42])
    assert bot.send_message.await_count == 2
    assert "восстановлена" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_irrelevant_node_does_not_notify():
    node = Node(
        id="worker-a",
        display_name="Worker A",
        state="ready",
        last_seen_at=100.0,
    )
    manager = SimpleNamespace(
        list_nodes=lambda: [Node.local(), node],
        get_user_settings=lambda _uid: {"bg_notify_node_status": True},
        get_selected_node_id=lambda _uid: "local",
        active_sessions_by_node={42: {}},
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    monitor = NodeNotificationMonitor(manager=manager, now=lambda: 161.0)

    await monitor.poll_once(bot, [42])

    bot.send_message.assert_not_awaited()
