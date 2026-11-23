"""One durable, bounded retry chain per local OpenCode prompt."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Bot
    from .input_recovery import InputRequest
    from .session_models import Session
    from .session_monitor import NewMessage

from .config import config
from .opencode_tls_proof import Attempt, digest, inspect_attempt
from .utils import atomic_write_json

logger = logging.getLogger(__name__)
DELAY = 12.0
LIMIT = 10


@dataclass
class Chain:
    request: str
    window: str
    activated: float
    native: str = ""
    db: str = ""
    floor: int = 0
    payload_hash: str = ""
    count: int = 0
    phase: str = "admitted"
    error_id: str = ""
    error_text: str = ""
    timestamp: str = ""
    due: float = 0


class TLSRetries:
    def __init__(self, path: Path):
        self.path = path
        self.chains: dict[str, Chain] = {}
        self.hidden: dict[str, list[str]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.loaded = False

    def load(self) -> None:
        if not self.loaded:
            if self.path.is_file():
                state = json.loads(self.path.read_text())
                self.chains = {k: Chain(**v) for k, v in state["chains"].items()}
                self.hidden = state["hidden"]
            self.loaded = True

    def save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "chains": {k: asdict(v) for k, v in self.chains.items()},
                "hidden": self.hidden,
            },
        )

    def admit(self, sess: Session, request: str) -> None:
        if sess.backend == "opencode" and sess.node_id == "local":
            self.load()
            self.chains[sess.id] = Chain(request, sess.window_id, sess.activated_at)
            self.save()

    def cancel(self, sess: Session | None) -> None:
        if (
            sess is not None
            and getattr(sess, "backend", None) == "opencode"
            and sess.node_id == "local"
        ):
            self.load()
            if self.chains.pop(sess.id, None):
                self.save()

    def prepare(
        self, request: InputRequest, checkpoint: dict[str, Any], text: str
    ) -> None:
        self.load()
        chain = self.chains.get(request.session_id)
        if (
            not checkpoint
            or not chain
            or chain.request != request.id
            or request.kind not in ("voice", "text")
        ):
            return
        chain.phase = "sent"
        chain.db = checkpoint["db"]
        chain.native = checkpoint.get("session_id", "")
        chain.floor = checkpoint["rowid"]
        chain.payload_hash = digest(text)
        self.save()

    def progress(self, sess: Session) -> int:
        self.load()
        chain = self.chains.get(sess.id)
        return chain.count if chain and chain.phase in ("waiting", "sent") else 0

    def hidden_on_card(self, sess: Session, mid: str, api_error: str) -> bool:
        self.load()
        if mid in self.hidden.get(sess.id, ()):
            return True
        # Seeding on the user event can read the error one callback ahead.
        chain = self.chains.get(sess.id)
        return bool(
            mid
            and api_error == "opencode_tls_handshake"
            and chain
            and chain.phase == "sent"
            and chain.count < LIMIT
            and self.owner(sess.id, chain)
            and self.proof(sess, chain, mid)
        )

    def hide(self, sid: str, mid: str) -> None:
        ids = self.hidden.setdefault(sid, [])
        if mid not in ids:
            ids.append(mid)
            del ids[:-256]

    def owner(self, sid: str, chain: Chain) -> Session | None:
        from .session import session_manager

        sess = session_manager.get_session(sid)
        if (
            sess
            and sess.backend == "opencode"
            and sess.node_id == "local"
            and sess.state in ("active", "idle")
            and sess.window_id == chain.window
            and sess.activated_at == chain.activated
            and (not chain.native or chain.native == sess.claude_session_id)
        ):
            return sess
        return None

    def proof(self, sess: Session, chain: Chain, error_id: str = "") -> Attempt | None:
        from .opencode_receipt import window_binding
        from .opencode_store import database_path

        binding = window_binding(chain.window)
        if (
            not binding
            or binding.get("session_id") != sess.claude_session_id
            or binding.get("cwd") != sess.workdir
            or Path(chain.db) != database_path().resolve()
        ):
            return None
        try:
            return inspect_attempt(
                chain.db,
                sess.claude_session_id,
                sess.workdir,
                chain.floor,
                chain.payload_hash,
                error_id,
            )
        except (OSError, ValueError, KeyError):
            return None

    @staticmethod
    def newer(chain: Chain, msg: NewMessage) -> bool:
        from .opencode_store import open_store

        try:
            with open_store(Path(chain.db)) as db:
                return (
                    db.execute(
                        "SELECT 1 FROM message WHERE id=? AND session_id=? AND rowid>?",
                        (msg.opencode_message_id, msg.session_id, chain.floor),
                    ).fetchone()
                    is not None
                )
        except (OSError, ValueError):
            return False

    async def event(self, sess: Session, msg: NewMessage, bot: Bot) -> bool:
        if (
            sess.backend != "opencode"
            or sess.node_id != "local"
            or not msg.opencode_message_id
        ):
            return False
        self.load()
        mid = msg.opencode_message_id
        if mid in self.hidden.get(sess.id, ()):
            return True
        chain = self.chains.get(sess.id)
        if not chain or chain.phase == "admitted" or self.owner(sess.id, chain) is None:
            return False
        if msg.role == "user":
            if chain.count and self.newer(chain, msg):
                current = self.proof(sess, chain)
                if not current or current.user != mid:
                    self.cancel(sess)
                elif chain.phase == "sent":
                    self.hide(sess.id, mid)
                    self.save()
                    return True
            return False
        if (
            msg.role != "assistant"
            or msg.content_type != "text"
            or msg.stop_reason not in ("end_turn", "max_tokens", "stop_sequence")
        ):
            return False
        if msg.api_error != "opencode_tls_handshake":
            # Old transcript events can arrive after the next request was admitted.
            if self.newer(chain, msg):
                self.cancel(sess)
            return False
        if chain.phase != "sent":
            return False
        if not self.proof(sess, chain, mid):
            if self.newer(chain, msg):
                self.cancel(sess)
            return False
        if chain.count >= LIMIT:
            self.cancel(sess)
            return False
        chain.native = msg.session_id
        chain.count += 1
        chain.phase, chain.error_id, chain.timestamp, chain.error_text = (
            "waiting",
            mid,
            msg.timestamp,
            msg.text,
        )
        chain.due = time.time() + DELAY
        self.hide(sess.id, mid)
        self.save()
        self.start(sess.id, bot)
        return True

    def start(self, sid: str, bot: Bot) -> None:
        if sid not in self.tasks or self.tasks[sid].done():
            self.tasks[sid] = asyncio.create_task(self.run(sid, bot))

    async def refresh(self, sess: Session, bot: Bot) -> None:
        from .handlers.notifications import refresh_panel, is_active_for_user
        from .session import session_manager

        for uid, _ in session_manager.all_user_sessions_with_claude_id(
            sess.claude_session_id
        ):
            if is_active_for_user(uid, sess):
                await refresh_panel(bot, uid, immediate=True)

    async def run(self, sid: str, bot: Bot) -> None:
        try:
            while (chain := self.chains.get(sid)) and chain.count:
                sess = self.owner(sid, chain)
                if not sess:
                    self.chains.pop(sid)
                    self.save()
                    return
                if chain.phase == "sent":
                    # A crash after reserving paste is ambiguous. Never retype.
                    if self.proof(sess, chain):
                        return
                    chain.phase = "report"
                    self.save()
                if chain.phase == "report":
                    await self.report(sess, chain, bot)
                    continue
                try:
                    await asyncio.wait_for(self.refresh(sess, bot), timeout=2.0)
                except Exception:
                    logger.exception(
                        "OpenCode retry card refresh failed session=%s", sid
                    )
                await asyncio.sleep(max(0, chain.due - time.time()))
                if self.chains.get(sid) is not chain or not self.owner(sid, chain):
                    continue
                error = chain.error_id
                try:
                    ok = await self.send(sess, chain)
                except Exception:
                    logger.exception("OpenCode retry transport failed session=%s", sid)
                    ok = False
                if self.chains.get(sid) is not chain or chain.error_id != error:
                    continue
                if chain.phase == "sent" and (ok or self.proof(sess, chain)):
                    return
                chain.phase = "report"
                self.save()
        finally:
            self.tasks.pop(sid, None)

    async def send(self, sess: Session, chain: Chain) -> bool:
        from .opencode_input import on_prompt_paste, PasteNotSent
        from .tmux_manager import tmux_manager

        error = chain.error_id
        attempt = self.proof(sess, chain, error)
        if not attempt:
            return False

        def before_paste() -> None:
            if (
                self.chains.get(sess.id) is not chain
                or not self.owner(sess.id, chain)
                or self.proof(sess, chain, error) != attempt
            ):
                raise PasteNotSent()
            chain.floor = attempt.error_row
            chain.phase = "sent"
            self.save()

        with on_prompt_paste(chain.window, before_paste):
            return await tmux_manager.send_keys(
                chain.window, attempt.text, backend="opencode"
            )

    async def report(self, sess: Session, chain: Chain, bot: Bot) -> None:
        from .bot.session_events import handle_new_message
        from .session_monitor import NewMessage

        ids = self.hidden.get(sess.id, [])
        if chain.error_id in ids:
            ids.remove(chain.error_id)
        self.save()
        await handle_new_message(
            NewMessage(
                chain.native,
                chain.error_text,
                True,
                stop_reason="end_turn",
                api_error="opencode_request_failed",
                timestamp=chain.timestamp,
            ),
            bot,
        )
        if self.chains.get(sess.id) is chain:
            self.cancel(sess)

    def restore(self, bot: Bot) -> None:
        self.load()
        for sid, chain in self.chains.items():
            if chain.count:
                self.start(sid, bot)

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


tls_retries = TLSRetries(config.config_dir / "opencode-tls-retries.json")
