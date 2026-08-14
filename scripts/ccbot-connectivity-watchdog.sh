#!/usr/bin/env bash
# Restart the OGameScene ccbot containers after a prolonged proxy outage.

set -euo pipefail

proxy_url="${PROXY_URL:-http://172.17.0.1:1081}"
check_url="${CHECK_URL:-https://api.telegram.org/}"
fail_after_sec="${FAIL_AFTER_SEC:-1800}"
state_dir="${STATE_DIR:-/var/lib/ccbot-connectivity-watchdog}"
containers="${CONTAINERS:-ccbot ccbot2}"
dry_run="${DRY_RUN:-0}"
read -r -a container_names <<< "$containers"

failed_since_file="$state_dir/failed_since"
restarted_file="$state_dir/restarted_for_outage"
lock_file="$state_dir/watchdog.lock"

mkdir -p "$state_dir"
exec 9>"$lock_file"
flock -n 9 || exit 0

log() {
    logger -t ccbot-connectivity-watchdog -- "$*"
    printf '%s\n' "$*"
}

if curl --silent --show-error --max-time 20 \
    --proxy "$proxy_url" --output /dev/null "$check_url"; then
    if [ -e "$failed_since_file" ] || [ -e "$restarted_file" ]; then
        log "connectivity restored; outage state cleared"
    fi
    rm -f "$failed_since_file" "$restarted_file"
    exit 0
fi

now="$(date +%s)"
if [ ! -s "$failed_since_file" ]; then
    printf '%s\n' "$now" > "$failed_since_file"
    log "connectivity unavailable; starting ${fail_after_sec}s grace period"
    exit 0
fi

failed_since="$(cat "$failed_since_file")"
case "$failed_since" in
    ''|*[!0-9]*)
        printf '%s\n' "$now" > "$failed_since_file"
        log "invalid outage timestamp reset; starting ${fail_after_sec}s grace period"
        exit 0
        ;;
esac

elapsed=$((now - failed_since))
if [ "$elapsed" -lt "$fail_after_sec" ]; then
    log "connectivity still unavailable for ${elapsed}s; restart threshold is ${fail_after_sec}s"
    exit 0
fi

if [ -e "$restarted_file" ]; then
    log "connectivity still unavailable; hard restart already performed for this outage"
    exit 0
fi

if [ "$dry_run" = "1" ]; then
    log "DRY RUN: would hard-restart containers after ${elapsed}s outage: ${containers}"
else
    # A zero-second timeout deliberately escalates immediately if SIGTERM does
    # not stop the container. Persistent Docker volumes preserve bot state.
    docker restart --timeout 0 "${container_names[@]}"
    log "hard-restarted containers after ${elapsed}s outage: ${containers}"
fi
printf '%s\n' "$now" > "$restarted_file"
