"""Project the common native message/part shape while retaining OpenCode identity."""

from typing import Any

from .transcript_zcode import project_snapshot as project_native_snapshot


def project_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows = project_native_snapshot(snapshot)
    for row in rows:
        if row.get("type") == "zcode_usage":
            row["type"] = "opencode_usage"
        if row.get("error") == "zcode_request_failed":
            row["error"] = "opencode_request_failed"
    return rows
