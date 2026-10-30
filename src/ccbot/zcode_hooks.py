"""Install native ZCode lifecycle hooks without touching Claude or Codex settings."""

from __future__ import annotations

import json
from pathlib import Path

from .utils import atomic_write_json


def install_hooks(command: str, settings_file: Path | None = None) -> int:
    path = settings_file or Path.home() / ".zcode/cli/config.json"
    try:
        settings = json.loads(path.read_text()) if path.exists() else {}
        hooks = settings.setdefault("hooks", {})
        events = hooks.setdefault("events", {})
        hooks["enabled"] = True
        for event in ("SessionStart", "UserPromptSubmit"):
            entries = events.setdefault(event, [])
            installed = any(
                hook.get("command") == command
                and hook.get("args") == ["hook", "--backend", "zcode"]
                for entry in entries
                for hook in entry.get("hooks", [])
            )
            if not installed:
                entries.append(
                    {
                        "hooks": [
                            {
                                "type": "process",
                                "command": command,
                                "args": ["hook", "--backend", "zcode"],
                                "timeoutMs": 5000,
                            }
                        ]
                    }
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, settings)
    except (OSError, ValueError, AttributeError, TypeError):
        return 1
    return 0
