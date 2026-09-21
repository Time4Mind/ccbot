"""Schedule one authoritative remote-session reconciliation per worker boot."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_boot_ids: dict[str, str] = {}
_tasks: dict[str, asyncio.Task[None]] = {}


def schedule_remote_reconcile(manager: Any, node_id: str, boot_id: str) -> None:
    if not boot_id or _boot_ids.get(node_id) == boot_id:
        return
    _boot_ids[node_id] = boot_id
    active = _tasks.get(node_id)
    if active is not None and not active.done():
        active.cancel()

    async def run() -> None:
        from .session_recovery import reconcile_remote_sessions
        from .transfer_runtime import get_node_runtime

        runtime = get_node_runtime(node_id)
        if runtime is None:
            return
        try:
            rebound, lost = await reconcile_remote_sessions(manager, node_id, runtime)
            logger.info(
                "Remote session reconcile complete node=%s rebound=%d lost=%d",
                node_id,
                rebound,
                lost,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Remote session reconcile failed node=%s", node_id)

    task = asyncio.create_task(run(), name=f"node-reconcile-{node_id}")
    _tasks[node_id] = task
    task.add_done_callback(
        lambda done: _tasks.pop(node_id, None) if _tasks.get(node_id) is done else None
    )


async def shutdown_remote_reconcile() -> None:
    tasks = list(_tasks.values())
    _tasks.clear()
    _boot_ids.clear()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
