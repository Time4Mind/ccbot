"""Issue #334: connectivity, backend readiness and operation admission."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery, User

from ccbot.bot import _session_create
from ccbot.bot.commands import lifecycle
from ccbot.bot.callbacks import dir_browser, transfer
from ccbot.handlers.callback_data import CB_DIR_UP
from ccbot.handlers.directory_browser import BROWSE_NODE_KEY, BROWSE_PATH_KEY
from ccbot.node_backend_readiness import update_backend_health
from ccbot.node_models import Node
from ccbot.remote_prompt_queue import _default_node_available
from ccbot.session import SessionManager
from ccbot.startup_queue import (
    begin_startup_queue,
    has_startup_queue,
    reset_startup_queues_for_test,
)


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    return SessionManager()


@pytest.mark.parametrize(
    "state, age, enabled, connected, selectable, available",
    [
        ("online", 44.9, True, True, True, False),
        ("online", 45.0, True, False, False, False),
        ("ready", 44.9, True, True, True, True),
        ("ready", 45.0, True, False, False, False),
        ("pending", 0.0, True, False, False, False),
        ("offline", 0.0, True, False, False, False),
        ("online", 0.0, False, True, False, False),
        ("ready", 0.0, False, True, False, False),
    ],
)
def test_connectivity_and_new_work_admission_have_independent_semantics(
    state, age, enabled, connected, selectable, available
):
    node = Node("worker", "Worker", state=state, enabled=enabled, last_seen_at=100.0)
    assert node.is_connected(now=100.0 + age) is connected
    assert node.is_selectable(now=100.0 + age) is selectable
    assert node.is_available(now=100.0 + age) is available


@pytest.mark.parametrize("state", ["pending", "online", "ready"])
def test_no_first_health_does_not_admit_selection_or_work(state):
    node = Node("worker", "Worker", state=state)
    assert not node.is_connected(now=100.0)
    assert not node.is_selectable(now=100.0)
    assert not node.is_available(now=100.0)


def test_local_connectivity_does_not_expire():
    node = Node.local()
    assert node.is_connected(now=10_000.0)
    assert node.is_selectable(now=10_000.0)
    assert node.is_available(now=10_000.0)


def test_backend_health_changes_only_ready_provider_and_admission(manager):
    node = Node("worker", "Worker", state="online", last_seen_at=time.time())
    manager.register_node(node)
    for ready in ([], ["codex"], ["claude", "codex"], ["codex"], []):
        update_backend_health(
            node,
            {
                "configured_backends": ["claude", "codex"],
                "backends": ready,
                "backend_status": {
                    backend: "ready" if backend in ready else "unavailable"
                    for backend in ("claude", "codex")
                },
            },
        )
        node.state = "ready" if ready else "online"
        node.last_seen_at = time.time()
        assert node.is_connected()
        assert node.is_selectable()
        assert manager.get_effective_backends(42, node.id) == tuple(ready)


def test_local_readiness_snapshot_with_no_ready_backends_is_authoritative(manager):
    manager.user_settings[42] = {"enabled_backends": ["claude", "codex"]}
    node = manager.get_node("local")
    assert node is not None
    update_backend_health(
        node,
        {
            "configured_backends": ["claude", "codex"],
            "backends": [],
            "backend_status": {"claude": "unavailable", "codex": "unavailable"},
        },
    )
    assert manager.get_effective_backends(42, "local") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_directory_browsing_depends_on_capability_not_backend_auth(
    monkeypatch, manager, allowed
):
    node = Node(
        "worker",
        "Worker",
        state="online",
        last_seen_at=time.time(),
        configured_backends=["codex"],
        capabilities={"directory_browser": allowed},
    )
    manager.register_node(node)
    runtime = SimpleNamespace(
        list_directories=AsyncMock(
            return_value={"path": "/work", "directories": ["project"]}
        )
    )
    monkeypatch.setattr(dir_browser, "session_manager", manager)
    monkeypatch.setattr(dir_browser, "get_node_runtime", lambda _node_id: runtime)
    context = SimpleNamespace(user_data={})
    if allowed:
        _text, _keyboard, directories = await dir_browser.initialize_directory_browser(
            context, 42, node_id=node.id
        )
        assert directories == ["project"]
        runtime.list_directories.assert_awaited_once_with(node.id, "")
    else:
        with pytest.raises(RuntimeError, match="Directory browsing is unavailable"):
            await dir_browser.initialize_directory_browser(context, 42, node_id=node.id)
        runtime.list_directories.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_creation_and_transfer_do_not_admit_unready_backend(
    monkeypatch, manager
):
    node = Node(
        "worker",
        "Worker",
        state="online",
        last_seen_at=time.time(),
        configured_backends=["codex"],
    )
    manager.register_node(node)
    query = MagicMock(spec=CallbackQuery)
    query.answer = AsyncMock()
    user = MagicMock(spec=User)
    user.id = 42
    context = SimpleNamespace(
        user_data={"_new_session_node_id": node.id, "_new_session_backend": "codex"},
        bot=object(),
    )
    runtime = SimpleNamespace(create_session=AsyncMock())
    edit = AsyncMock()
    monkeypatch.setattr(_session_create, "session_manager", manager)
    monkeypatch.setattr(_session_create, "get_node_runtime", lambda _node_id: runtime)
    monkeypatch.setattr(_session_create, "safe_edit", edit)

    await _session_create.create_and_activate_session(
        query, context, user, "/work", node_id=node.id
    )

    runtime.create_session.assert_not_awaited()
    assert "Backend is no longer available" in edit.await_args.args[1]
    source = manager.create_session(name="Source", backend="claude")
    with pytest.raises(ValueError, match="Target backend is unavailable"):
        manager.start_context_transfer(
            source_session_id=source.id,
            target_node_id=node.id,
            target_backend="codex",
            context_path="/work/context.md",
        )


@pytest.mark.parametrize("capability", ["create_session", "context_transfer"])
def test_new_session_and_transfer_preserve_operation_capabilities(
    monkeypatch, manager, capability
):
    node = Node(
        "worker",
        "Worker",
        state="ready",
        last_seen_at=time.time(),
        backends=["codex"],
        capabilities={capability: False},
    )
    manager.register_node(node)
    source = manager.create_session(name="Source", backend="claude")
    monkeypatch.setattr(transfer, "session_manager", manager)
    manager.set_active_session(42, source.id)
    with pytest.raises(ValueError, match="unavailable"):
        manager.start_context_transfer(
            source_session_id=source.id,
            target_node_id=node.id,
            target_backend="codex",
            context_path="/work/context.md",
        )
    assert "tr:node:worker" not in [
        button.callback_data
        for row in transfer.build_transfer_node_keyboard(42).inline_keyboard
        for button in row
    ]


@pytest.mark.parametrize(
    "state, capabilities, admitted",
    [
        ("online", {"event_stream": True, "send_text": True}, True),
        ("online", {"event_stream": False, "send_text": True}, False),
        ("ready", {"send_text": False}, False),
    ],
)
def test_existing_session_queue_does_not_wait_for_new_backend_auth(
    monkeypatch, manager, state, capabilities, admitted
):
    node = Node(
        "worker",
        "Worker",
        state=state,
        last_seen_at=time.time(),
        capabilities=capabilities,
    )
    manager.register_node(node)
    monkeypatch.setattr("ccbot.session.session_manager", manager)
    assert _default_node_available(node.id) is admitted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled, last_seen, capabilities",
    [
        (False, None, {"directory_browser": True}),
        (True, 1.0, {"directory_browser": True}),
        (True, None, {"directory_browser": False}),
    ],
)
async def test_directory_callback_rechecks_connection_and_capability(
    monkeypatch, manager, enabled, last_seen, capabilities
):
    node = Node(
        "worker",
        "Worker",
        state="online",
        enabled=enabled,
        last_seen_at=time.time() if last_seen is None else last_seen,
        capabilities=capabilities,
    )
    manager.register_node(node)
    query = SimpleNamespace(data=CB_DIR_UP, answer=AsyncMock())
    context = SimpleNamespace(
        user_data={BROWSE_NODE_KEY: node.id, BROWSE_PATH_KEY: "/work/project"}
    )
    runtime = SimpleNamespace(list_directories=AsyncMock())
    monkeypatch.setattr(dir_browser, "session_manager", manager)
    monkeypatch.setattr(dir_browser, "get_node_runtime", lambda _nid: runtime)
    assert await dir_browser.handle(query, context, SimpleNamespace(id=42))
    query.answer.assert_awaited_once_with(
        "Directory browsing is unavailable on this node", show_alert=True
    )
    runtime.list_directories.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_ready_backend_rejects_new_flow_without_leaving_input_captured(
    monkeypatch, manager
):
    node = Node(
        "worker",
        "Worker",
        state="online",
        last_seen_at=time.time(),
        configured_backends=["codex"],
    )
    manager.register_node(node)
    manager.set_selected_node(42, node.id)
    monkeypatch.setattr(dir_browser, "session_manager", manager)
    edit = AsyncMock()
    monkeypatch.setattr(dir_browser, "safe_edit", edit)
    reset_startup_queues_for_test()
    try:
        begin_startup_queue(42)
        await dir_browser.open_new_session_flow(
            SimpleNamespace(), SimpleNamespace(user_data={}), 42, origin="main"
        )
        assert "no available backend" in edit.await_args.args[1]
        assert not has_startup_queue(42)
    finally:
        reset_startup_queues_for_test()


@pytest.mark.asyncio
async def test_new_command_without_ready_backend_cancels_startup_capture(
    monkeypatch, manager
):
    node = Node(
        "worker",
        "Worker",
        state="online",
        last_seen_at=time.time(),
        configured_backends=["codex"],
    )
    manager.register_node(node)
    manager.set_selected_node(42, node.id)
    monkeypatch.setattr(lifecycle, "session_manager", manager)
    monkeypatch.setattr(lifecycle, "is_user_allowed", lambda _uid: True)
    reply = AsyncMock()
    monkeypatch.setattr(lifecycle, "safe_reply", reply)
    reset_startup_queues_for_test()
    try:
        await lifecycle.new_command(
            SimpleNamespace(
                effective_user=SimpleNamespace(id=42),
                message=SimpleNamespace(text="/new"),
            ),
            SimpleNamespace(user_data={}),
        )
        assert "backend" in reply.await_args.args[1]
        assert not has_startup_queue(42)
    finally:
        reset_startup_queues_for_test()
