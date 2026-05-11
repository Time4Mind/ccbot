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
# Behaviour:
#   1. Probe ``$CCBOT_NET_PROBE_URL`` (default api.telegram.org) every
#      ``$CCBOT_NET_RETRY_SEC`` (default 5s) until the request returns
#      any HTTP status. 000 / timeout = still down, keep waiting.
#   2. When the probe succeeds, exec ``uv run ccbot`` in the foreground.
#   3. When that exits — for ANY reason (TimedOut, KeyboardInterrupt,
#      crash, deliberate /stop) — wait ``$CCBOT_RESTART_BACKOFF`` seconds
#      (default 10s) and go back to step 1.
#
# Run it directly inside a tmux pane (e.g. ``ccbot:__main__``) or wire
# it into runit / cron @reboot / a profile.d hook — see
# ``doc/install-linux.md`` for the chroot-on-Android variant.
#
# Env knobs:
#   CCBOT_NET_PROBE_URL    URL to curl until it responds. Default:
#                          https://api.telegram.org/
#   CCBOT_NET_RETRY_SEC    Sleep between net-probe attempts. Default: 5
#   CCBOT_RESTART_BACKOFF  Sleep after the bot exits before retrying.
#                          Default: 10
#   CCBOT_UV               Path to the uv binary. Default: ``uv`` on PATH.

set -u

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

probe_url="${CCBOT_NET_PROBE_URL:-https://api.telegram.org/}"
net_retry="${CCBOT_NET_RETRY_SEC:-5}"
restart_backoff="${CCBOT_RESTART_BACKOFF:-10}"
uv_bin="${CCBOT_UV:-uv}"

if ! command -v "$uv_bin" > /dev/null 2>&1; then
    # Fall back to the typical Linux install path; helps when the
    # supervisor is launched by runit / cron with a stripped PATH.
    if [ -x "/root/.local/bin/uv" ]; then
        uv_bin="/root/.local/bin/uv"
    elif [ -x "$HOME/.local/bin/uv" ]; then
        uv_bin="$HOME/.local/bin/uv"
    fi
fi

log() {
    printf '[%s] supervisor: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

log "starting (project=$PROJECT_DIR uv=$uv_bin probe=$probe_url)"

while true; do
    # Wait for Telegram API reachability. ``-o /dev/null`` discards the
    # body; we only care that *something* came back. ``-s`` keeps the
    # log clean, ``--max-time`` caps any one probe at ~8s so a single
    # hung connection doesn't pin the loop.
    until curl -s --max-time 8 -o /dev/null "$probe_url" > /dev/null 2>&1; do
        log "telegram unreachable, retrying in ${net_retry}s"
        sleep "$net_retry"
    done

    log "telegram reachable, starting ccbot"
    "$uv_bin" run ccbot
    rc=$?
    log "ccbot exited rc=${rc}, restarting in ${restart_backoff}s"
    sleep "$restart_backoff"
done
