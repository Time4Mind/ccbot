"""Optional Telegram notifications for prolonged remote-node outages."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Iterable

NODE_NOTIFICATION_DELAY_SECONDS = 60.0


class NodeNotificationMonitor:
    def __init__(
        self,
        *,
        manager: Any,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._manager = manager
        self._now = now
        self._notified: set[tuple[int, str]] = set()

    async def poll_once(self, bot: Any, user_ids: Iterable[int]) -> None:
        now = self._now()
        for user_id in user_ids:
            enabled = bool(
                self._manager.get_user_settings(user_id).get(
                    "bg_notify_node_status", False
                )
            )
            if not enabled:
                self._notified = {key for key in self._notified if key[0] != user_id}
                continue
            selected = self._manager.get_selected_node_id(user_id)
            active_nodes = set(self._manager.active_sessions_by_node.get(user_id, {}))
            for node in self._manager.list_nodes():
                if node.id == "local" or (
                    node.id != selected and node.id not in active_nodes
                ):
                    continue
                key = (user_id, node.id)
                available = node.is_available(now=now)
                if available:
                    if key in self._notified:
                        await bot.send_message(
                            chat_id=user_id,
                            text=f"✅ Нода {node.display_name or node.id} восстановлена.",
                        )
                        self._notified.remove(key)
                    continue
                prolonged = (
                    node.last_seen_at > 0
                    and now - node.last_seen_at >= NODE_NOTIFICATION_DELAY_SECONDS
                )
                if prolonged and key not in self._notified:
                    await bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"⚠️ Нода {node.display_name or node.id} недоступна "
                            "более минуты. Ожидающие запросы остаются в FIFO до 15 минут."
                        ),
                    )
                    self._notified.add(key)

    async def run(
        self,
        bot: Any,
        user_ids: Iterable[int],
        *,
        poll_interval: float = 15.0,
    ) -> None:
        while True:
            await self.poll_once(bot, user_ids)
            await asyncio.sleep(poll_interval)


__all__ = ["NODE_NOTIFICATION_DELAY_SECONDS", "NodeNotificationMonitor"]
