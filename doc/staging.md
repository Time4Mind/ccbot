# Local Telegram staging bot

This deployment is intentionally isolated from the production bot:

- checkout: `/Users/a-s-nosko/pet_projects/ccbot-staging`
- state: `/Users/a-s-nosko/.ccbot-staging`
- Codex home: `/Users/a-s-nosko/.codex-staging`
- tmux session: `ccbot-staging`
- launchd label: `com.ccbot.staging`
- Telegram token: a separate BotFather bot
- process home / directory-browser root: `/Users/a-s-nosko/.ccbot-staging/workspaces`

The manager refuses to run when any of these paths point at production.

## Bootstrap

```bash
cd /Users/a-s-nosko/pet_projects/ccbot-staging
./scripts/ccbot-staging.sh install
```

Put the staging bot token in `/Users/a-s-nosko/.ccbot-staging/.env`, then:

```bash
chmod 600 /Users/a-s-nosko/.ccbot-staging/.env
./scripts/ccbot-staging.sh doctor
./scripts/ccbot-staging.sh start
./scripts/ccbot-staging.sh status
```

The first Codex session may require a separate login because staging uses an
isolated `CODEX_HOME`. Do not copy production auth or rollout state into it.

## Operations

```bash
./scripts/ccbot-staging.sh restart
./scripts/ccbot-staging.sh logs
./scripts/ccbot-staging.sh stop
```

Production `com.ccbot`, `~/.ccbot`, the `ccbot` tmux session, and the primary
checkout are outside this script's targets.

## Snapshot production archives

The importer copies complete JSONL records into staging and creates an archived
Session record whose workdir is under the staging home. It never resumes against
the production rollout file or production workdir. Stop staging before import:

```bash
./scripts/ccbot-staging.sh stop
.venv/bin/python scripts/staging_import_archives.py --name "gmw summary" --allow-live-snapshot
./scripts/ccbot-staging.sh start
```

Live production sessions require the explicit `--allow-live-snapshot` flag. The
result is a point-in-time copy; subsequent staging turns append only to the copy.
