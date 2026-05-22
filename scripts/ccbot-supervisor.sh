#!/usr/bin/env bash
# ccbot-supervisor: long-running wrapper that keeps `uv run ccbot` alive.
#
# Intended for chroot / no-systemd environments (the typical Termux/
# NetHunter setup on Android, where the bot lives inside a Kali chroot
# under runsvdir). systemd's `Restart=always` is unavailable there, and
# the bot's first call to ``getMe`` will hard-fail if the upstream VPN/
# proxy is momentarily down — leaving the user with a dead Python
# process until they manually restart it. This script puts a thin
# wait-for-network loop in front of every (re)start so VPN flaps cost
# only a backoff, not a manual recovery.
#
# This is the ROUTINE / idempotent path: running it at any time is safe.
# It is NOT a restarter — it never preempts a healthy instance. The
# deliberate restart-over-a-running-bot path is ``scripts/restart.sh``.
#
# Order of checks is ALWAYS process-level first, network-level second:
#   1. Take an exclusive flock on ``$CCBOT_DIR/ccbot-supervisor.lock`` so
#      only ONE supervisor ever runs. A second supervisor exits cleanly
#      (rc=0) instead of fighting the first — the historical failure mode
#      was several stranded supervisor loops each hammering ``uv run
#      ccbot`` every ~10s for a full day (bug A2b).
#   2. PROCESS GATE (authoritative, FIRST). Probe ``$CCBOT_DIR/ccbot.lock``
#      with ``flock(1)``. The bot holds this lock (via fcntl.flock) for
#      its whole lifetime and the kernel releases it the instant the
#      holder dies — so "held" ⟺ "a healthy bot is live", no pidfile
#      needed. If HELD → a healthy instance wins: back off with an
#      EXPONENTIAL, capped delay and re-probe. Never enter wait-for-net,
#      never launch a duplicate, never kill. After ``$CCBOT_HELD_GIVEUP``
#      cumulative seconds of "still held" the supervisor exits cleanly:
#      the other instance owns the bot.
#   3. NETWORK CHECK (SECOND, only after winning the process gate). Probe
#      ``$CCBOT_NET_PROBE_URL`` (default api.telegram.org) every
#      ``$CCBOT_NET_RETRY_SEC`` (default 5s) until the request returns any
#      HTTP status. 000 / timeout = still down (VPN flap), keep waiting.
#   4. When net is reachable AND the lock is free, run ``uv run ccbot`` in
#      the foreground.
#   5. When that exits, back off per the bot's exit-code contract
#      (see ``src/ccbot/main.py``):
#        - rc=0 (EXIT_CLEAN) → clean stop OR a clean yield (the in-process
#          flock saw another healthy holder in a start race). Either way
#          do NOT restart-promptly: loop back to the top, where the
#          process gate re-probes and backs off if a holder is live.
#        - rc!=0 (EXIT_CRASH) → genuine crash / misconfig → restart
#          promptly after ``$CCBOT_RESTART_BACKOFF`` seconds.
#      A run that stayed up longer than ``$CCBOT_HEALTHY_RUN_SEC`` resets
#      the exponential backoff back to its floor.
#
# Run it directly inside a tmux pane (e.g. ``ccbot:__main__``) or wire
# it into runit / cron @reboot / a profile.d hook — see
# ``doc/install-linux.md`` for the chroot-on-Android variant.
#
# Env knobs:
#   CCBOT_NET_PROBE_URL    URL to curl until it responds. Default:
#                          https://api.telegram.org/
#   CCBOT_NET_RETRY_SEC    Sleep between net-probe attempts. Default: 5
#   CCBOT_RESTART_BACKOFF  Base sleep after a crash before retrying.
#                          Default: 10
#   CCBOT_BACKOFF_MAX      Cap for the exponential backoff. Default: 300
#   CCBOT_HELD_GIVEUP      Cumulative seconds of "lock held by another
#                          instance" before this supervisor exits cleanly.
#                          Default: 600
#   CCBOT_HEALTHY_RUN_SEC  A run longer than this resets the backoff to
#                          its floor. Default: 120
#   CCBOT_DIR              Config dir holding the lock files. Default:
#                          ``$HOME/.ccbot``.
#   CCBOT_UV               Path to the uv binary. Default: ``uv`` on PATH.

set -u

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

probe_url="${CCBOT_NET_PROBE_URL:-https://api.telegram.org/}"
net_retry="${CCBOT_NET_RETRY_SEC:-5}"
restart_backoff="${CCBOT_RESTART_BACKOFF:-10}"
backoff_max="${CCBOT_BACKOFF_MAX:-300}"
held_giveup="${CCBOT_HELD_GIVEUP:-600}"
healthy_run_sec="${CCBOT_HEALTHY_RUN_SEC:-120}"
uv_bin="${CCBOT_UV:-uv}"
ccbot_dir="${CCBOT_DIR:-$HOME/.ccbot}"
bot_lock="${ccbot_dir}/ccbot.lock"
supervisor_lock="${ccbot_dir}/ccbot-supervisor.lock"

if ! command -v "$uv_bin" > /dev/null 2>&1; then
    # Fall back to the typical install location for ``curl -LsSf ... | sh``.
    # Helps when the supervisor is launched by a service manager with a
    # stripped PATH that doesn't include ``$HOME/.local/bin``.
    if [ -x "$HOME/.local/bin/uv" ]; then
        uv_bin="$HOME/.local/bin/uv"
    fi
fi

log() {
    printf '[%s] supervisor: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

# --- Single-supervisor guard (bug A2b) -------------------------------------
# Hold an exclusive flock on a dedicated supervisor lock for our whole
# lifetime so exactly one supervisor body runs. We open fd 9 onto the
# lock file and ``flock -n`` it; the fd stays open for the process
# lifetime, releasing automatically on exit. ``flock -n`` returns 1
# immediately when another supervisor already holds it — we map that to
# a clean exit (rc 0) so a stray second launch (manual + @reboot, two
# tmux panes, …) doesn't spin up a competing restart loop.
mkdir -p "$ccbot_dir"
if ! command -v flock > /dev/null 2>&1; then
    log "WARNING: flock(1) unavailable — cannot guard against duplicate supervisors"
else
    exec 9> "$supervisor_lock"
    if ! flock -n 9; then
        log "another supervisor already holds ${supervisor_lock}; exiting cleanly"
        exit 0
    fi
fi

# ``bot_lock_held`` — true (rc 0) when another live process holds the
# bot's singleton flock. flock(1) and Python's fcntl.flock share the
# same flock(2) lock family, so this is an accurate, non-destructive
# probe. ``-E 42`` makes "could not acquire" exit 42 (distinct from a
# missing ``flock`` binary or a probe error).
bot_lock_held() {
    [ -e "$bot_lock" ] || return 1
    command -v flock > /dev/null 2>&1 || return 1
    flock -n -E 42 "$bot_lock" -c true > /dev/null 2>&1
    [ "$?" -eq 42 ]
}

log "starting (project=$PROJECT_DIR uv=$uv_bin probe=$probe_url)"

backoff="$restart_backoff"
held_elapsed=0

# ``held_backoff`` — exponential, capped sleep while a healthy holder owns
# the bot lock. Updates ``backoff`` / ``held_elapsed`` in place and exits
# the whole supervisor cleanly once the holder has owned the lock past
# ``$CCBOT_HELD_GIVEUP``. Used both before launch (process gate) and after
# a clean yield, so the back-off accounting stays in one place.
held_backoff() {
    log "ccbot.lock held by a healthy instance; backing off ${backoff}s (held ${held_elapsed}/${held_giveup}s)"
    sleep "$backoff"
    held_elapsed=$((held_elapsed + backoff))
    backoff=$((backoff * 2))
    [ "$backoff" -gt "$backoff_max" ] && backoff="$backoff_max"
    if [ "$held_elapsed" -ge "$held_giveup" ]; then
        log "another instance has owned ccbot.lock for >=${held_giveup}s; this supervisor exits cleanly"
        exit 0
    fi
}

while true; do
    # --- 1. PROCESS GATE (authoritative, FIRST) ---------------------------
    # Before touching the network or launching anything, ask the only
    # question that matters: is a healthy bot already alive? The bot's
    # singleton flock answers it (kernel frees the lock on holder death).
    # If HELD → a healthy instance wins; back off quietly and re-probe.
    # Never enter wait-for-net, never launch, never preempt.
    if bot_lock_held; then
        held_backoff
        continue
    fi

    # --- 2. NETWORK CHECK (SECOND, only after winning the process gate) ---
    # Wait for Telegram API reachability. ``-o /dev/null`` discards the
    # body; we only care that *something* came back. ``-s`` keeps the
    # log clean, ``--max-time`` caps any one probe at ~8s so a single
    # hung connection doesn't pin the loop.
    until curl -s --max-time 8 -o /dev/null "$probe_url" > /dev/null 2>&1; do
        log "telegram unreachable, retrying in ${net_retry}s"
        sleep "$net_retry"
    done

    # --- 3. LAUNCH --------------------------------------------------------
    log "telegram reachable, starting ccbot"
    started_at="$(date +%s)"
    "$uv_bin" run ccbot
    rc=$?
    ran_for=$(( $(date +%s) - started_at ))

    # A long, healthy run resets the exponential backoff to its floor.
    if [ "$ran_for" -ge "$healthy_run_sec" ]; then
        backoff="$restart_backoff"
        held_elapsed=0
    fi

    # --- 4. POST-EXIT, per the bot's exit-code contract -------------------
    # rc=0 (EXIT_CLEAN) → clean stop OR a clean yield (the in-process flock
    # saw another healthy holder during a start race). Do NOT restart
    # promptly: loop back to the top, where the process gate re-probes and
    # backs off if a holder is in fact live. If the lock is free it was a
    # genuine clean stop and the next iteration relaunches after wait-for-net.
    if [ "$rc" -eq 0 ]; then
        if bot_lock_held; then
            log "ccbot exited cleanly (rc=0) — another instance holds the lock; yielding"
        else
            log "ccbot exited cleanly (rc=0) after ${ran_for}s; re-checking and relaunching"
        fi
        continue
    fi

    # rc!=0 (EXIT_CRASH) → genuine crash / misconfig → restart promptly.
    log "ccbot crashed (rc=${rc}) after ${ran_for}s, restarting in ${restart_backoff}s"
    sleep "$restart_backoff"
done
