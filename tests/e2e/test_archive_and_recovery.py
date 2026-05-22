"""E2E: archive teardown + startup recovery against the real helpers.

Two independent scenarios:

  * archive → window kill → orphan cleanup: ``commands.lifecycle.archive_session``
    on a live Session must ``kill_window`` then ``kill_orphan_claude_processes``,
    and flip the Session record to archived (dropping the active pointer).

  * startup stale-ID resolution: after a tmux server restart, persisted
    ``window_states`` keys point at dead window ids. ``resolve_stale_ids`` must
    re-map a stale id onto the live window that shares its display name, and
    drop one with no live counterpart.
"""

from __future__ import annotations

import pytest

from ccbot.bot.commands.lifecycle import archive_session
from ccbot.session import session_manager

from harness import USER_ID, seed_session

WINDOW_ID = "@100"
WORKDIR = "/tmp/proj"
CLAUDE_SID = "88888888-8888-8888-8888-888888888888"


@pytest.mark.asyncio
async def test_archive_session_kills_window_and_orphans(fake_tmux, fake_bot):
    fake_tmux.add_window(WINDOW_ID, name="proj", cwd=WORKDIR)
    sess = seed_session(
        session_manager,
        sid="ffff8888",
        name="proj",
        window_id=WINDOW_ID,
        workdir=WORKDIR,
        claude_session_id=CLAUDE_SID,
        active_for=USER_ID,
    )

    await archive_session(USER_ID, fake_bot, sess, completed=False)

    # tmux window killed, then orphan claude --resume processes mopped up.
    assert WINDOW_ID in fake_tmux.killed
    assert CLAUDE_SID in fake_tmux.orphans_killed

    # Session record flipped to archived; active pointer dropped (no
    # replacement available).
    assert session_manager.sessions["ffff8888"].state == "archived"
    assert session_manager.sessions["ffff8888"].window_id == ""
    assert session_manager.get_active_session(USER_ID) is None


@pytest.mark.asyncio
async def test_archive_completed_tags_done(fake_tmux, fake_bot):
    fake_tmux.add_window(WINDOW_ID, name="proj", cwd=WORKDIR)
    sess = seed_session(
        session_manager,
        sid="ffff8888",
        name="proj",
        window_id=WINDOW_ID,
        workdir=WORKDIR,
        claude_session_id=CLAUDE_SID,
        active_for=USER_ID,
    )

    await archive_session(USER_ID, fake_bot, sess, completed=True)

    assert session_manager.sessions["ffff8888"].state == "completed"
    assert WINDOW_ID in fake_tmux.killed


@pytest.mark.asyncio
async def test_resolve_stale_ids_remaps_by_display_name(fake_tmux):
    # Persisted window_state under a STALE id (@100) whose display name is
    # "proj". After a tmux restart the same window is now @300; a second
    # stale id (@101 / "gone") has no live counterpart and must be dropped.
    ws = session_manager.get_window_state("@100")
    ws.cwd = WORKDIR
    ws.session_id = CLAUDE_SID
    ws.window_name = "proj"
    session_manager.window_display_names["@100"] = "proj"

    ws2 = session_manager.get_window_state("@101")
    ws2.cwd = "/tmp/gone"
    ws2.session_id = "99999999-9999-9999-9999-999999999999"
    ws2.window_name = "gone"
    session_manager.window_display_names["@101"] = "gone"

    # Live tmux now exposes "proj" at a NEW id; "gone" no longer exists.
    fake_tmux.add_window("@300", name="proj", cwd=WORKDIR)

    await session_manager.resolve_stale_ids()

    # @100 re-mapped to @300; its window-state carried over.
    assert "@300" in session_manager.window_states
    assert session_manager.window_states["@300"].session_id == CLAUDE_SID
    assert "@100" not in session_manager.window_states
    assert session_manager.window_display_names.get("@300") == "proj"
    # The orphaned @101 (no live window) was dropped.
    assert "@101" not in session_manager.window_states
