"""Report OpenCode's recorded session usage while keeping unknown account quotas unknown."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

from . import codex_usage
from .codex_auth import cached_chatgpt_account_id
from .opencode_runtime import oauth_account_id

from .opencode_session_io import build_session_file_path, reported_usage


@dataclass(frozen=True)
class OpenCodeUsageInfo:
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str = ""
    codex_quota: codex_usage.CodexUsageInfo | None = None
    codex_account: str | None = field(default=None, repr=False)


def _shared_account() -> str | None:
    account = oauth_account_id()
    return account if account and account == cached_chatgpt_account_id() else None


def shared_quota_for_user(
    user_id: int, usage: OpenCodeUsageInfo
) -> codex_usage.CodexUsageInfo | None:
    """Revalidate the account before displaying a previously fetched quota."""
    from .session import session_manager

    if (
        usage.codex_quota is not None
        and session_manager.get_selected_node_id(user_id) == "local"
        and usage.codex_account == _shared_account()
        and usage.codex_account
    ):
        return usage.codex_quota
    return None


async def fetch_usage() -> OpenCodeUsageInfo:
    from .session import session_manager

    def collect() -> OpenCodeUsageInfo:
        total = incoming = outgoing = 0
        seen: set[str] = set()
        model = ""
        found = False
        for session in session_manager.sessions.values():
            if (
                session.backend != "opencode"
                or not session.claude_session_id
                or session.claude_session_id in seen
                or session.node_id != "local"
            ):
                continue
            seen.add(session.claude_session_id)
            path = build_session_file_path(session.claude_session_id, session.workdir)
            if path is None:
                continue
            data = reported_usage(path)
            if not data["model"]:
                continue
            found = True
            model = data["model"]
            total += data["total_tokens"]
            incoming += data["input_tokens"]
            outgoing += data["output_tokens"]
        return OpenCodeUsageInfo(
            total if found else None,
            incoming if found else None,
            outgoing if found else None,
            model,
        )

    info = await asyncio.to_thread(collect)
    account = _shared_account()
    if account is None:
        return info
    # A transcript may contain quota from a previous login.
    quota = await codex_usage.fetch_codex_usage(allow_rollout_fallback=False)
    if quota is not None and account == _shared_account():
        return replace(info, codex_quota=quota, codex_account=account)
    return info
