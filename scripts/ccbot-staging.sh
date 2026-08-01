#!/usr/bin/env bash
# Manage the isolated local Telegram staging bot on macOS.
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
staging_dir="${CCBOT_STAGING_DIR:-/Users/a-s-nosko/.ccbot-staging}"
staging_codex_home="${CCBOT_STAGING_CODEX_HOME:-/Users/a-s-nosko/.codex-staging}"
label="com.ccbot.staging"
domain="gui/$(id -u)"
plist_path="/Users/a-s-nosko/Library/LaunchAgents/${label}.plist"
template_path="${project_dir}/scripts/com.ccbot.staging.plist.template"
env_path="${staging_dir}/.env"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

service_loaded() {
    launchctl print "${domain}/${label}" >/dev/null 2>&1
}

bot_lock_free() {
    "${project_dir}/.venv/bin/python" - "${staging_dir}/ccbot.lock" <<'PY'
import fcntl
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a+") as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(1) from None
    fcntl.flock(handle, fcntl.LOCK_UN)
PY
}

require_isolation() {
    [ "$project_dir" = "/Users/a-s-nosko/pet_projects/ccbot-staging" ] \
        || die "refusing to run outside the staging worktree: ${project_dir}"
    [ "$staging_dir" = "/Users/a-s-nosko/.ccbot-staging" ] \
        || die "unexpected staging dir: ${staging_dir}"
    [ "$staging_codex_home" = "/Users/a-s-nosko/.codex-staging" ] \
        || die "unexpected staging CODEX_HOME: ${staging_codex_home}"
    [ "$staging_dir" != "/Users/a-s-nosko/.ccbot" ] \
        || die "staging CCBOT_DIR points at production"
    [ "$staging_codex_home" != "/Users/a-s-nosko/.codex" ] \
        || die "staging CODEX_HOME points at production"
}

require_credentials() {
    [ -f "$env_path" ] || die "missing ${env_path}"
    local token users
    token="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$env_path" | tail -1)"
    users="$(sed -n 's/^ALLOWED_USERS=//p' "$env_path" | tail -1)"
    [ -n "$token" ] || die "TELEGRAM_BOT_TOKEN is empty in ${env_path}"
    [ -n "$users" ] || die "ALLOWED_USERS is empty in ${env_path}"
    [ "$(stat -f '%Lp' "$env_path")" = "600" ] \
        || die "${env_path} must have mode 600"
    "${project_dir}/.venv/bin/python" - "$env_path" "/Users/a-s-nosko/.ccbot/.env" <<'PY'
import hmac
import sys

from dotenv import dotenv_values

staging = str(dotenv_values(sys.argv[1]).get("TELEGRAM_BOT_TOKEN") or "")
production = str(dotenv_values(sys.argv[2]).get("TELEGRAM_BOT_TOKEN") or "")
if production and hmac.compare_digest(staging, production):
    raise SystemExit("ERROR: staging and production Telegram tokens are identical")
PY
}

validate_telegram() {
    "${project_dir}/.venv/bin/python" - "$env_path" <<'PY'
import sys

import httpx
from dotenv import dotenv_values

values = dotenv_values(sys.argv[1])
token = str(values.get("TELEGRAM_BOT_TOKEN") or "")
proxy = str(values.get("TG_PROXY_URL") or "") or None
try:
    with httpx.Client(proxy=proxy, timeout=8.0) as client:
        response = client.get(f"https://api.telegram.org/bot{token}/getMe")
        payload = response.json()
except Exception as exc:
    raise SystemExit(
        f"ERROR: Telegram staging token check failed: {type(exc).__name__}"
    ) from None
if not response.is_success or not payload.get("ok"):
    raise SystemExit("ERROR: Telegram staging token was rejected")
username = str((payload.get("result") or {}).get("username") or "unknown")
print(f"Telegram API: connected as @{username}")
PY
}

render_plist() {
    mkdir -p "$(dirname "$plist_path")"
    sed \
        -e "s|__PROJECT_DIR__|${project_dir}|g" \
        -e "s|__STAGING_DIR__|${staging_dir}|g" \
        -e "s|__CODEX_HOME__|${staging_codex_home}|g" \
        "$template_path" >"${plist_path}.tmp"
    plutil -lint "${plist_path}.tmp" >/dev/null
    mv "${plist_path}.tmp" "$plist_path"
    chmod 600 "$plist_path"
}

install_staging() {
    require_isolation
    mkdir -p \
        "$staging_dir/logs" \
        "$staging_dir/tmux" \
        "$staging_dir/workspaces" \
        "$staging_codex_home"
    chmod 700 \
        "$staging_dir" \
        "$staging_dir/logs" \
        "$staging_dir/tmux" \
        "$staging_dir/workspaces" \
        "$staging_codex_home"
    if [ ! -f "$env_path" ]; then
        cp "${project_dir}/scripts/staging.env.example" "$env_path"
    fi
    chmod 600 "$env_path"
    /opt/homebrew/bin/uv sync --all-extras
    render_plist
    echo "Installed stopped staging service: ${plist_path}"
    echo "Runtime: ${staging_dir}"
    echo "Codex home: ${staging_codex_home}"
}

start_staging() {
    require_isolation
    require_credentials
    [ -x "${project_dir}/.venv/bin/ccbot" ] || die "run install first"
    [ -f "$plist_path" ] || die "run install first"
    validate_telegram
    if service_loaded; then
        launchctl kickstart -k "${domain}/${label}"
    else
        launchctl bootstrap "$domain" "$plist_path"
        launchctl kickstart -k "${domain}/${label}"
    fi
    echo "Started ${label}"
}

stop_staging() {
    require_isolation
    if service_loaded; then
        launchctl bootout "${domain}/${label}"
        local waited=0
        while ! bot_lock_free && [ "$waited" -lt 50 ]; do
            sleep 0.2
            waited=$((waited + 1))
        done
        bot_lock_free || die "${label} stopped but its lock is still held"
        echo "Stopped ${label}; lock released"
    else
        bot_lock_free || die "${label} is unloaded but its lock is still held"
        echo "${label} is already stopped; lock is free"
    fi
}

status_staging() {
    require_isolation
    echo "Isolation:"
    echo "  project=${project_dir}"
    echo "  state=${staging_dir}"
    echo "  codex_home=${staging_codex_home}"
    echo "  tmux=ccbot-staging"
    echo "  launchd=${label}"
    if service_loaded; then
        launchctl print "${domain}/${label}" | sed -n '1,45p'
    else
        echo "Service: stopped"
    fi
}

doctor_staging() {
    require_isolation
    [ -f "$plist_path" ] || die "missing installed plist"
    plutil -lint "$plist_path" >/dev/null
    [ -x "${project_dir}/.venv/bin/ccbot" ] || die "staging venv is missing"
    [ ! -e "/Users/a-s-nosko/.ccbot-staging/ccbot.lock" ] || {
        [ "/Users/a-s-nosko/.ccbot-staging/ccbot.lock" != "/Users/a-s-nosko/.ccbot/ccbot.lock" ] \
            || die "lock collision"
    }
    if [ -f "$env_path" ]; then
        chmod 600 "$env_path"
    fi
    echo "Isolation checks passed."
    if [ -f "$env_path" ] && [ -n "$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$env_path" | tail -1)" ]; then
        require_credentials
        validate_telegram
        echo "Credentials: configured"
    else
        echo "Credentials: waiting for TELEGRAM_BOT_TOKEN"
    fi
}

logs_staging() {
    touch "$staging_dir/logs/launchd.log"
    tail -n 100 -f "$staging_dir/logs/launchd.log"
}

case "${1:-status}" in
    install) install_staging ;;
    start) start_staging ;;
    stop) stop_staging ;;
    restart) stop_staging; start_staging ;;
    status) status_staging ;;
    doctor) doctor_staging ;;
    logs) logs_staging ;;
    *) die "usage: $0 {install|start|stop|restart|status|doctor|logs}" ;;
esac
