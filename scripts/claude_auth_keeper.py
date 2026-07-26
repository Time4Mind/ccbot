#!/usr/bin/env python3
"""Watch Claude Code OAuth credentials and keep them alive as far as possible.

Measured behaviour of the credential store (``$CLAUDE_CONFIG_DIR/.credentials.json``)
on 2026-07-26, Claude Code v2.1.220:

* ``expiresAt`` — access token, 8 h TTL. ANY real API call refreshes it
  (``claude -p 'ok' --model haiku`` is the cheapest trigger we found).
  ``claude auth status`` does NOT: it reports ``loggedIn: true`` from the mere
  presence of the file, even with an access token that expired 16 h ago — so it
  is useless both as a health probe and as a refresh trigger.
* ``refreshTokenExpiresAt`` — the hard wall. Each refresh *rotates* the refresh
  token (new value) but keeps the SAME absolute deadline, i.e. ~30 days from the
  last interactive login. Keep-alive traffic cannot push it out.

Consequence: the monthly re-consent cannot be automated away (that is the point
of an OAuth consent screen). What CAN be automated is everything around it —
noticing the deadline early, keeping a dormant host warm, and driving the login
exchange itself so the human part shrinks to "tap link, approve, paste code".

Modes:
  --check      classify every credential store on this host (exit 0/1/2)
  --keepalive  refresh a stale access token where the wall is still ahead
  --notify     push a Telegram alert per band crossing (7d / 3d / 24h / dead)
  --relay      run ``claude auth login`` piped, print the URL, feed back a code
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Bands (seconds before refreshTokenExpiresAt) that trigger an alert once each.
BANDS: tuple[tuple[str, float], ...] = (
    ("7d", 7 * 86400),
    ("3d", 3 * 86400),
    ("24h", 86400),
    ("dead", 0.0),
)

CHEAP_PROBE = ("claude", "-p", "ok", "--model", "haiku")
URL_RE = re.compile(r"https://claude\.com/cai/oauth/authorize\?[^\s\x1b\]]+")
CODE_PROMPT = "Paste code here if prompted"


@dataclass
class Store:
    """One Claude Code credential store (one CLAUDE_CONFIG_DIR)."""

    label: str
    config_dir: Path
    access_exp: float | None = None
    wall: float | None = None
    subscription: str = "?"
    error: str = ""

    @property
    def wall_left(self) -> float:
        return (self.wall or 0.0) - time.time()

    @property
    def access_stale(self) -> bool:
        return self.access_exp is not None and self.access_exp <= time.time()

    @property
    def state(self) -> str:
        if self.error:
            return "error"
        if self.wall is None:
            return "error"
        left = self.wall_left
        if left <= 0:
            return "dead"
        if left <= 86400:
            return "critical"
        if left <= 3 * 86400:
            return "warn"
        if left <= 7 * 86400:
            return "notice"
        return "ok"


def _fmt(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _human(seconds: float) -> str:
    if seconds <= 0:
        return "expired"
    d, rem = divmod(int(seconds), 86400)
    h = rem // 3600
    return f"{d}d {h}h" if d else f"{h}h"


def discover_stores(extra: list[str] | None = None) -> list[Store]:
    """Default store plus every CLAUDE_CONFIG_DIR referenced by a ccbot .env."""
    seen: dict[Path, str] = {}
    home_default = Path.home() / ".claude"
    seen[home_default] = "default"

    for env_file in sorted(Path.home().glob(".ccbot*/.env")):
        try:
            text = env_file.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("CLAUDE_CONFIG_DIR="):
                path = Path(line.split("=", 1)[1].strip().strip("\"'")).expanduser()
                seen.setdefault(path, env_file.parent.name)

    for raw in extra or []:
        seen.setdefault(Path(raw).expanduser(), Path(raw).name)

    return [Store(label=label, config_dir=path) for path, label in seen.items()]


def load(store: Store) -> Store:
    creds = store.config_dir / ".credentials.json"
    try:
        blob = json.loads(creds.read_text())
    except FileNotFoundError:
        store.error = "no credentials file"
        return store
    except (OSError, json.JSONDecodeError) as exc:
        store.error = f"unreadable: {exc}"
        return store

    oauth = blob.get("claudeAiOauth") or {}
    if not oauth:
        store.error = "not an OAuth store (API key / 3P provider?)"
        return store
    access = oauth.get("expiresAt")
    wall = oauth.get("refreshTokenExpiresAt")
    store.access_exp = access / 1000 if access else None
    store.wall = wall / 1000 if wall else None
    store.subscription = str(oauth.get("subscriptionType") or "?")
    return store


def probe_env(store: Store) -> dict[str, str]:
    """Env for a probe: isolated hooks, no inherited tmux/session identity."""
    env = dict(os.environ)
    env.pop("TMUX", None)
    env["CLAUDE_CONFIG_DIR"] = str(store.config_dir)
    env["CCBOT_DIR"] = "/tmp/ccbot-auth-keeper"
    env["IS_SANDBOX"] = "1"
    return env


def keepalive(store: Store, timeout: float = 180) -> str:
    """Force an access-token refresh. Returns a short outcome string."""
    if store.state in ("dead", "error"):
        return "skipped (wall gone)"
    if not store.access_stale:
        return f"not needed (access valid until {_fmt(store.access_exp)})"
    before = store.access_exp
    try:
        proc = subprocess.run(
            CHEAP_PROBE,
            env=probe_env(store),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"probe failed: {exc}"
    after = load(Store(label=store.label, config_dir=store.config_dir)).access_exp
    if after and before and after > before:
        return f"refreshed -> {_fmt(after)}"
    return f"no refresh (rc={proc.returncode}) {proc.stderr.strip()[:120]}"


def _tg_credentials(env_file: Path) -> tuple[str, list[str]]:
    token, users = "", []
    for line in env_file.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.split("=", 1)[1].strip()
        elif line.startswith("ALLOWED_USERS="):
            users = [u.strip() for u in line.split("=", 1)[1].split(",") if u.strip()]
    return token, users


def notify(text: str, env_file: Path) -> bool:
    token, users = _tg_credentials(env_file)
    if not token or not users:
        print(f"notify: no token/users in {env_file}", file=sys.stderr)
        return False
    ok = True
    for chat_id in users:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            with urllib.request.urlopen(url, data=payload, timeout=20) as resp:
                ok = ok and resp.status == 200
        except OSError as exc:
            print(f"notify {chat_id} failed: {exc}", file=sys.stderr)
            ok = False
    return ok


def band_for(store: Store) -> str | None:
    """Alert band, or None when the store is healthy or not OAuth-backed.

    A store with no ``claudeAiOauth`` block (API-key / third-party provider,
    e.g. the GLM rollback config) has no wall to warn about — without this
    guard ``wall_left`` would be hugely negative and every run would claim
    the auth is dead.
    """
    if store.error or store.wall is None:
        return None
    left = store.wall_left
    for name, threshold in BANDS:
        if left <= threshold:
            return name
    return None


def run_check(stores: list[Store]) -> int:
    worst = 0
    rank = {"ok": 0, "notice": 1, "warn": 1, "critical": 2, "dead": 2, "error": 2}
    for store in stores:
        left = f"({_human(store.wall_left)} left)" if store.wall else ""
        print(
            f"{store.label:<10} {store.state:<8} "
            f"wall={_fmt(store.wall)} {left}  "
            f"access={_fmt(store.access_exp)}"
            f"{' STALE' if store.access_stale else ''}  "
            f"sub={store.subscription}  {store.error}".rstrip()
        )
        worst = max(worst, rank.get(store.state, 2))
    return worst


def run_notify(stores: list[Store], env_file: Path, state_file: Path) -> None:
    try:
        seen = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError):
        seen = {}
    host = os.environ.get("CCBOT_HOST", os.uname().nodename)
    changed = False
    for store in stores:
        band = band_for(store)
        if band is None:
            if seen.pop(store.label, None) is not None:
                changed = True
            continue
        if seen.get(store.label) == band:
            continue
        seen[store.label] = band
        changed = True
        if band == "dead":
            text = (
                f"❌ [{host}] Claude auth DEAD ({store.label})\n"
                f"refresh token expired {_fmt(store.wall)} — "
                f"re-login required, every session on this host is failing."
            )
        else:
            text = (
                f"⚠️ [{host}] Claude auth expires in {_human(store.wall_left)} "
                f"({store.label})\n"
                f"wall: {_fmt(store.wall)} — re-login while you're at a browser. "
                f"Keep-alive cannot extend it."
            )
        notify(text, env_file)
    if changed:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(seen, indent=2))


def _spawn_pty(argv: tuple[str, ...], env: dict[str, str], cols: int = 400):
    """Start a child on a pty. Piped stdout is block-buffered by the CLI (and the
    login prompt carries no newline), so a pipe never streams; a wide pty also
    stops the OAuth URL from being wrapped across lines."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, cols, 0, 0))
    proc = subprocess.Popen(
        argv, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True
    )
    os.close(slave)
    return proc, master


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>]"
)


def _drain(master: int, until: float, stop: re.Pattern[str] | str) -> str:
    """Read from the pty until `stop` shows up in the cleaned buffer or we time out."""
    buf = ""
    while time.time() < until:
        ready, _, _ = select.select([master], [], [], 0.5)
        if not ready:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += _ANSI_RE.sub("", chunk.decode(errors="replace"))
        hit = stop.search(buf) if isinstance(stop, re.Pattern) else (stop in buf)
        if hit:
            break
    return buf


def run_relay(store: Store, code_file: Path | None, env_file: Path | None) -> int:
    """Drive ``claude auth login`` and hand the URL out / the code back in."""
    proc, master = _spawn_pty(("claude", "auth", "login"), probe_env(store))
    buf = _drain(master, time.time() + 90, URL_RE)
    match = URL_RE.search(buf)
    url = match.group(0) if match else ""
    if not url:
        proc.kill()
        print(
            f"relay: no OAuth URL captured\n--- pane ---\n{buf[-800:]}", file=sys.stderr
        )
        return 1

    print(f"AUTH URL:\n{url}\n")
    if env_file:
        notify(
            f"🔐 Claude re-login ({store.label}). Open, approve, send the code back:\n{url}",
            env_file,
        )

    if code_file:
        print(f"waiting for code in {code_file} (10 min)…")
        code = ""
        wait_until = time.time() + 600
        while time.time() < wait_until and not code:
            if code_file.exists():
                code = code_file.read_text().strip()
                code_file.unlink(missing_ok=True)
            else:
                time.sleep(2)
    else:
        code = input("paste code > ").strip()
    if not code:
        proc.kill()
        return 1

    os.write(master, (code + "\r").encode())
    rest = _drain(master, time.time() + 120, "Login successful")
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(rest.strip()[-500:])
    after = load(Store(label=store.label, config_dir=store.config_dir))
    print(f"new wall: {_fmt(after.wall)} ({_human(after.wall_left)} left)")
    if env_file:
        notify(
            f"✅ Claude re-login ok ({store.label}) — wall now {_fmt(after.wall)}",
            env_file,
        )
    return 0 if after.state == "ok" else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="classify every store")
    ap.add_argument(
        "--keepalive", action="store_true", help="refresh stale access tokens"
    )
    ap.add_argument(
        "--notify", action="store_true", help="Telegram alert on band crossings"
    )
    ap.add_argument(
        "--relay", metavar="LABEL", help="run the login exchange for this store"
    )
    ap.add_argument(
        "--store", action="append", default=[], help="extra CLAUDE_CONFIG_DIR"
    )
    ap.add_argument(
        "--env-file",
        default=str(Path.home() / ".ccbot" / ".env"),
        help="ccbot .env supplying TELEGRAM_BOT_TOKEN / ALLOWED_USERS",
    )
    ap.add_argument("--code-file", help="--relay: read the pasted code from this file")
    ap.add_argument(
        "--state-file",
        default=str(Path.home() / ".ccbot" / "auth_keeper_state.json"),
        help="alert dedup state",
    )
    args = ap.parse_args()
    # Cron / the bot capture stdout through a pipe, where Python block-buffers:
    # the relay's URL would then sit invisible in the buffer until exit.
    sys.stdout.reconfigure(line_buffering=True)

    stores = [load(s) for s in discover_stores(args.store)]

    if args.relay:
        match = [s for s in stores if s.label == args.relay]
        if not match:
            print(f"unknown store {args.relay!r}", file=sys.stderr)
            return 2
        return run_relay(
            match[0],
            Path(args.code_file) if args.code_file else None,
            Path(args.env_file) if args.notify else None,
        )

    rc = 0
    if args.check or not (args.keepalive or args.notify):
        rc = run_check(stores)
    if args.keepalive:
        for store in stores:
            if store.error:
                continue
            print(f"{store.label:<10} keepalive: {keepalive(store)}")
    if args.notify:
        run_notify(stores, Path(args.env_file), Path(args.state_file))
    return rc


if __name__ == "__main__":
    sys.exit(main())
