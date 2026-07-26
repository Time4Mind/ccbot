# Claude Code auth lifecycle — measured, and what can be automated

Field research for the "re-authorise without me" ask: what actually expires in
a Claude Code OAuth login, what a script can renew unattended, and where a
human is structurally required.

Measured on 2026-07-26, Claude Code **v2.1.220**, Max subscription, two
credential stores on one host (`~/.claude` and the isolated
`CLAUDE_CONFIG_DIR=~/.claude-glm` used by the second ccbot instance).

## The credential store

`$CLAUDE_CONFIG_DIR/.credentials.json` → `claudeAiOauth`:

| Field | Meaning | Measured |
| --- | --- | --- |
| `accessToken` / `expiresAt` | working token | **8 h** TTL |
| `refreshToken` / `refreshTokenExpiresAt` | renewal chain | **~30 days from the last interactive login** |
| `scopes`, `subscriptionType`, `rateLimitTier` | account facts | `max`, `default_claude_max_20x` |

## Three findings that shape the design

**1. `claude auth status` lies.** It reported `loggedIn: true` on a store whose
access token had expired 16 h earlier, and it does **not** trigger a refresh.
Useless both as a health probe and as a renewal trigger — parse the JSON file
instead.

**2. Any real API call refreshes the access token.** The cheapest trigger found
is `claude -p 'ok' --model haiku`: access `expiresAt` moved to `now + 8 h`.

**3. Refresh rotates, but the deadline does not move.** Same probe, same store:

```
refresh_sha  eabedc1251fb -> bfdc76f44aa6     (rotated)
refresh_exp  2026-08-16 04:33:52 -> 04:33:51  (unchanged)
```

The refresh token is replaced on every use while the **absolute wall stays
put**. Keep-alive traffic cannot extend it. Both stores on this host show the
same shape — a wall exactly 30 days after their respective login events
(2026-07-17 and 2026-07-19).

**Consequence:** a fully unattended re-auth is impossible by construction — the
wall can only be pushed out by a fresh consent at claude.com. What is
automatable is everything around it.

## The login flow is relay-friendly

`claude auth login` with **no TTY** prints exactly:

```
Opening browser to sign in…
If the browser didn't open, visit: <OAuth URL>
Paste code here if prompted >
```

Properties that matter:

- `redirect_uri=https://platform.claude.com/oauth/code/callback` — **not** a
  localhost loopback. A phone browser completes the redirect on its own and is
  shown a code to copy. No tunnel, no port forwarding.
- PKCE (`code_challenge`) is held in the running process, so the code must be
  fed back to **that same process** — the relay cannot be split across two
  invocations.
- `Paste code here if prompted` is a stable anchor for a parser.
- `claude setup-token` runs the same shape (scope `user:inference`) and is the
  candidate for trading the monthly ritual for a yearly one. Its token TTL is
  **not yet verified** — it needs one human consent to measure; do it under a
  throwaway `CLAUDE_CONFIG_DIR` and read the resulting expiry.

### Pipe vs pty

Piped stdout is block-buffered by the CLI and the code prompt carries no
newline, so a `subprocess.PIPE` reader never streams — it sees nothing until
the process exits. Run the child on a **pty**, and set a wide window
(`TIOCSWINSZ`, ~400 cols) or the URL gets wrapped across lines. Under a pty the
flow is also a TUI (spinner frames, OSC-8 hyperlinks with the URL duplicated),
so strip ANSI/OSC-8 before matching.

## What the keeper does

`scripts/claude_auth_keeper.py` — no dependencies beyond the stdlib.

```bash
python3 scripts/claude_auth_keeper.py --check       # classify every store, exit 0/1/2
python3 scripts/claude_auth_keeper.py --keepalive   # renew a stale access token
python3 scripts/claude_auth_keeper.py --notify      # TG alert per band crossing
python3 scripts/claude_auth_keeper.py --relay default --code-file /tmp/code
```

- **Discovery** — the default `~/.claude` plus every `CLAUDE_CONFIG_DIR` found
  in `~/.ccbot*/.env`, so a multi-instance host is covered in one run.
- **Classification** — `ok` / `notice` (≤7 d) / `warn` (≤3 d) / `critical`
  (≤24 h) / `dead`, from `refreshTokenExpiresAt`. Stores with no
  `claudeAiOauth` block (API-key / third-party provider) are reported as
  `error` and never alerted on — otherwise they'd look permanently dead.
- **Keep-alive** — only when the access token is stale and the wall is still
  ahead; verifies the refresh actually happened. Runs with `TMUX` unset and
  `CCBOT_DIR` pointed at a scratch dir so the ccbot `SessionStart` hook cannot
  rewrite a live instance's `session_map.json`.
- **Alerts** — one Telegram message per band per store, deduped through
  `~/.ccbot/auth_keeper_state.json` (same pattern as `handlers/quota_alerts.py`).
- **Relay** — spawns the login on a pty, extracts the URL, sends it to the DM,
  waits for the code in a file, feeds it in, then reports the new wall.

## Human step, minimised

Once a month: tap the link, approve, copy the code, send it to the bot. The
alert arrives 7 days early, so it never has to happen at a bad moment. If
`setup-token` turns out to be long-lived, even that collapses to once a year.

## Wiring

`cron` is installed on this host but **not running** (Android chroot, runit
supervises only a few services). Preferred home is therefore a background task
inside the bot next to `quota_alerts_loop` — hourly `--check`, alerts through
the normal `safe_send` path — rather than a new daemon.
