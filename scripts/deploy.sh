#!/usr/bin/env bash
# deploy.sh — pull origin/main and restart ccbot in the tmux supervisor window.
#
# Usage: ./scripts/deploy.sh [ref]
#   ref — optional git ref to fetch (default: main)
#
# Exits non-zero on any failure (pull conflict, restart pane gone, etc.)
# so cron / wakeup hooks can fail loudly instead of silently leaving the
# bot on stale code.
set -euo pipefail

REF="${1:-main}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESTART_SH="${PROJECT_DIR}/scripts/restart.sh"

echo "▶ deploy.sh: project=${PROJECT_DIR} ref=${REF}"

cd "$PROJECT_DIR"

# Refuse to deploy from a dirty working tree — overwriting in-progress
# edits with a fast-forward is exactly the kind of "destructive shortcut"
# the user has flagged before.
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "✖ deploy: working tree is dirty; commit/stash before deploying."
    git status --short
    exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "$REF" ]; then
    echo "▶ switching ${CURRENT_BRANCH} → ${REF}"
    git checkout "$REF"
fi

echo "▶ git pull --ff-only origin ${REF}"
git pull --ff-only origin "$REF"

if [ ! -x "$RESTART_SH" ]; then
    echo "✖ deploy: ${RESTART_SH} missing or not executable"
    exit 1
fi

echo "▶ restart.sh"
"$RESTART_SH"

echo "✓ deploy: done @ $(git rev-parse --short HEAD)"
