import json

import pytest

from ccbot.monitor_state import TrackedSession
from ccbot.session_monitor import SessionInfo, SessionMonitor


@pytest.mark.asyncio
async def test_rebound_session_reads_unseen_events_once(tmp_path, make_jsonl_entry):
    path = tmp_path / "session.jsonl"
    path.write_text("", encoding="utf-8")
    monitor = SessionMonitor(projects_path=tmp_path, state_file=tmp_path / "state")
    monitor.state.update_session(TrackedSession("parent", str(path), 0))
    monitor._last_session_map = {"@1": "parent"}
    await monitor._detect_and_cleanup_changes({"@1": "child"})
    row = make_jsonl_entry(msg_type="user", content="new request")
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    await monitor._detect_and_cleanup_changes({"@1": "parent"})
    events = await monitor.check_for_updates(
        {"parent"}, session_infos=[SessionInfo("parent", path)]
    )
    assert [event.text for event in events] == ["new request"]
    assert (
        await monitor.check_for_updates(
            {"parent"}, session_infos=[SessionInfo("parent", path)]
        )
        == []
    )
