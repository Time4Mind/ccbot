#!/usr/bin/env bash
# restart.sh — the EXPLICIT, DELIBERATE restart path: cleanly stop the
# running ccbot and launch a fresh one in the same tmux pane.
#
# This is NOT the routine path. The routine, idempotent path is
# ``scripts/ccbot-supervisor.sh``, which NEVER preempts a healthy
# instance — it only (re)starts when the bot lock is free. restart.sh is
# the one and only path allowed to STOP a running instance, and only
# because the operator explicitly asked for a restart. It still does so
# GRACEFULLY: SIGTERM first, SIGKILL only as a last resort after a
# timeout, and it ABORTS rather than launching over a still-held lock.
#
# Bug A2d: an unclean restart used to fire the new ``uv run ccbot``
# before the old process had fully exited. The old one kept polling
# Telegram's exclusive getUpdates, so the two cross-fired
# ``Conflict: terminated by other getUpdates request`` storms until one
# died. The fix: SIGTERM the old instance, then POLL until BOTH (a) no
# ccbot process matches AND (b) the singleton flock on
# ``$CCBOT_DIR/ccbot.lock`` is actually free, before starting the new
# one. SIGKILL is escalated only after a timeout.
set -euo pipefail

TMUX_SESSION="ccbot"
TMUX_WINDOW="__main__"
TARGET="${TMUX_SESSION}:${TMUX_WINDOW}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAX_WAIT=20  # seconds to wait for the old process to fully exit + release lock

ccbot_dir="${CCBOT_DIR:-$HOME/.ccbot}"
bot_lock="${ccbot_dir}/ccbot.lock"

# Check if tmux session and window exist
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "Error: tmux session '$TMUX_SESSION' does not exist"
    exit 1
fi

if ! tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$TMUX_WINDOW"; then
    echo "Error: window '$TMUX_WINDOW' not found in session '$TMUX_SESSION'"
    exit 1
fi

# Detect a running ccbot process. We deliberately avoid `pstree` (not on
# macOS by default) and instead match on the process command line.
is_ccbot_running() {
    pgrep -f 'uv run ccbot|\.venv/bin/ccbot' > /dev/null 2>&1
}

ccbot_pids() {
    pgrep -f 'uv run ccbot|\.venv/bin/ccbot' 2>/dev/null
}

# ``bot_lock_free`` — true when nothing holds the singleton flock. The
# bot releases it on exit (fd close), so a free lock is the authoritative
# "old instance is fully gone" signal. flock(1) and Python's fcntl.flock
# share the same flock(2) family. ``-E 42`` makes "could not acquire"
# exit 42; if the lock file is absent or flock(1) is unavailable we treat
# the lock as free (fall back to the pgrep check alone).
bot_lock_free() {
    [ -e "$bot_lock" ] || return 0
    command -v flock > /dev/null 2>&1 || return 0
    flock -n -E 42 "$bot_lock" -c true > /dev/null 2>&1
    [ "$?" -ne 42 ]
}

# ``fully_stopped`` — old instance gone AND lock released.
fully_stopped() {
    ! is_ccbot_running && bot_lock_free
}

# Stop existing process if running
if is_ccbot_running; then
    echo "Found running ccbot process(es): $(ccbot_pids | tr '\n' ' ')"

    # Prefer a clean SIGTERM straight to the PIDs — the bot installs a
    # graceful shutdown that releases the flock on its way out. tmux C-c
    # only reaches the foreground pane process and misses a backgrounded
    # bot, so we signal the matched PIDs directly.
    echo "Sending SIGTERM to ccbot process(es)..."
    for pid in $(ccbot_pids); do
        kill -TERM "$pid" 2>/dev/null || true
    done

    # Poll until BOTH the process is gone and the flock is released.
    waited=0
    while ! fully_stopped && [ "$waited" -lt "$MAX_WAIT" ]; do
        sleep 1
        waited=$((waited + 1))
        if is_ccbot_running; then
            echo "  Waiting for process to exit... (${waited}s/${MAX_WAIT}s)"
        else
            echo "  Process gone; waiting for lock release... (${waited}s/${MAX_WAIT}s)"
        fi
    done

    # Escalate to SIGKILL only after the grace period.
    if ! fully_stopped; then
        echo "Did not fully stop after ${MAX_WAIT}s, sending SIGKILL..."
        for pid in $(ccbot_pids); do
            kill -9 "$pid" 2>/dev/null || true
        done
        # Give the kernel a moment to reap the process and drop the flock.
        kwaited=0
        while ! fully_stopped && [ "$kwaited" -lt 5 ]; do
            sleep 1
            kwaited=$((kwaited + 1))
        done
    fi

    if fully_stopped; then
        echo "Process stopped and lock released."
    else
        echo "Warning: ccbot may still be holding ${bot_lock} — the new"
        echo "instance will refuse to start until the lock frees. Aborting."
        exit 1
    fi
else
    echo "No ccbot process running."
    # Even with no matching process, refuse to start over a held lock.
    if ! bot_lock_free; then
        echo "Warning: ${bot_lock} is held by an unmatched process. Aborting."
        exit 1
    fi
fi

# Brief pause to let the shell settle
sleep 1

# Start ccbot
echo "Starting ccbot in $TARGET..."
tmux send-keys -t "$TARGET" "cd ${PROJECT_DIR} && uv run ccbot" Enter

# Verify startup and show logs
sleep 3
if is_ccbot_running; then
    echo "ccbot restarted successfully. Recent logs:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -20
    echo "----------------------------------------"
else
    echo "Warning: ccbot may not have started. Pane output:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -30
    echo "----------------------------------------"
    exit 1
fi
