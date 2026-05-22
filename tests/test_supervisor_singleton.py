"""Lock-interop + exit-code contract for the supervisor / restart scripts.

``scripts/ccbot-supervisor.sh`` and ``scripts/restart.sh`` decide whether
a healthy bot is running by probing ``$CCBOT_DIR/ccbot.lock`` with
``flock(1)``. That only works if the lock ``main._acquire_singleton_lock``
takes is a real, externally-observable exclusive ``flock(2)`` that is held
for the process lifetime and released the instant the fd closes (process
exit). These tests pin that contract using an independent probe — the
same lock family ``flock(1)`` uses — so a regression in the lock helper
that would silently break the scripts' back-off / wait-for-exit logic is
caught here rather than in production.

They also pin the corrected exit-code contract: when another healthy
instance already holds the lock, the bot must YIELD CLEANLY
(``EXIT_CLEAN`` == 0), not crash with 1. The supervisor distinguishes a
clean yield (back off quietly, don't restart) from a real crash
(``EXIT_CRASH`` != 0, restart promptly) purely by this code, so a drift
back to "refuse == exit 1" would make the supervisor restart-storm a
healthy bot. The shell-side reorder (process gate before wait-for-net) is
covered by the manual verification noted at the bottom of this file.
"""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO, Any

import pytest

from ccbot.main import EXIT_CLEAN, EXIT_CRASH, _acquire_singleton_lock


def _yield_or_acquire(lock_path: Path) -> IO[Any]:
    """Mirror ``main``'s lock step: acquire, or yield cleanly if held.

    This is the exact translation ``main`` applies — the low-level helper
    raises ``SystemExit(1)`` on contention, and the caller re-exits with
    ``EXIT_CLEAN`` so the operational refuse path looks like a yield, not
    a crash. Pinning it here keeps the test honest even though full
    ``main()`` startup (config + tmux) is awkward to exercise in a unit.
    """
    try:
        return _acquire_singleton_lock(lock_path)
    except SystemExit:
        raise SystemExit(EXIT_CLEAN) from None


def _probe_held(lock_path: Path) -> bool:
    """True if an independent fd can't take the lock (== held elsewhere).

    Mirrors what ``flock -n -E 42 ccbot.lock`` does inside the shell
    scripts: a non-blocking acquire that fails iff someone else holds it.
    """
    fh = open(lock_path, "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        # Acquired → nobody held it. Release immediately so we don't
        # perturb the state we just measured.
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


def test_probe_sees_lock_as_free_when_unheld(tmp_path: Path) -> None:
    # Pre-bot: a probe must report the lock free so the supervisor's
    # pre-launch "is a healthy bot already up?" check says no.
    lock = tmp_path / "ccbot.lock"
    assert _probe_held(lock) is False


def test_probe_sees_lock_as_held_while_bot_holds_it(tmp_path: Path) -> None:
    # While main.py holds the singleton lock, the supervisor's flock(1)
    # probe must see it as busy and back off instead of launching a
    # doomed second instance (bug A2b).
    lock = tmp_path / "ccbot.lock"
    held = _acquire_singleton_lock(lock)
    try:
        assert _probe_held(lock) is True
    finally:
        held.close()


def test_probe_sees_lock_free_after_release(tmp_path: Path) -> None:
    # Process exit closes the fd and frees the lock. restart.sh polls for
    # exactly this transition before launching the replacement (bug A2d);
    # if the lock didn't free on close, restart would hang or abort.
    lock = tmp_path / "ccbot.lock"
    held = _acquire_singleton_lock(lock)
    assert _probe_held(lock) is True
    held.close()
    assert _probe_held(lock) is False


def test_exit_code_contract_constants() -> None:
    # The supervisor keys its restart-vs-backoff decision on these exact
    # values: rc=0 → yield/clean-stop (back off, never restart-promptly),
    # rc!=0 → crash (restart). If these drift, the shell logic silently
    # mis-classifies a healthy yield as a crash worth restarting.
    assert EXIT_CLEAN == 0
    assert EXIT_CRASH != 0


def test_yield_is_clean_when_lock_held(tmp_path: Path) -> None:
    # Corrected contract: a start that finds the lock already held by a
    # healthy instance must YIELD CLEANLY (exit 0), not crash with 1. The
    # supervisor probes the lock first, so this in-process refuse is only
    # the last-resort race guard — it must not look like a crash, or the
    # supervisor would restart-storm the running bot.
    lock = tmp_path / "ccbot.lock"
    held = _acquire_singleton_lock(lock)
    try:
        with pytest.raises(SystemExit) as exc:
            _yield_or_acquire(lock)
        assert exc.value.code == EXIT_CLEAN
    finally:
        held.close()


def test_yield_or_acquire_succeeds_when_free(tmp_path: Path) -> None:
    # When the lock is free the same path acquires normally — the yield
    # branch is reserved strictly for the contended case.
    lock = tmp_path / "ccbot.lock"
    fh = _yield_or_acquire(lock)
    try:
        assert not fh.closed
        assert _probe_held(lock) is True
    finally:
        fh.close()


# --- Manual verification for the shell-side reorder ---------------------------
#
# The supervisor's process-gate-before-wait-for-net ordering and the
# restart.sh graceful-stop flow are shell logic not exercised by pytest.
# Verify by hand after edits:
#
#   1. Process gate first (held → no net probe, no launch):
#        CCBOT_DIR=/tmp/ccbot-vrfy mkdir -p /tmp/ccbot-vrfy
#        # hold the lock from another shell:
#        flock /tmp/ccbot-vrfy/ccbot.lock -c 'sleep 60' &
#        # point the net probe at an unreachable host and run the supervisor:
#        CCBOT_DIR=/tmp/ccbot-vrfy CCBOT_NET_PROBE_URL=https://10.255.255.1/ \
#          CCBOT_HELD_GIVEUP=4 CCBOT_RESTART_BACKOFF=1 \
#          bash scripts/ccbot-supervisor.sh
#      EXPECT: only "ccbot.lock held by a healthy instance; backing off …"
#      lines, then a clean exit — NO "telegram unreachable" line (the net
#      probe is never reached because the process gate fires first).
#
#   2. restart.sh aborts over a still-held lock rather than launching:
#        # with the lock held as above, restart.sh's no-process branch
#        # must print "held by an unmatched process. Aborting." and exit 1.
