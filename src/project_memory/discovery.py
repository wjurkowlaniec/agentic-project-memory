from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def claude_project_dir(root: Path, home: Path | None = None) -> Path:
    """Return Claude Code's deterministic local directory for one project."""
    canonical = Path(root).expanduser().resolve()
    base = Path(home).expanduser() if home is not None else Path.home()
    return base / ".claude" / "projects" / str(canonical).replace("/", "-")


def devin_database(home: Path | None = None, data_home: Path | None = None) -> Path:
    base = Path(home).expanduser() if home is not None else Path.home()
    data = Path(data_home).expanduser() if data_home is not None else Path(os.environ.get("XDG_DATA_HOME", base / ".local" / "share"))
    return data / "devin" / "cli" / "sessions.db"


def discover_sources(
    root: Path,
    *,
    home: Path | None = None,
    data_home: Path | None = None,
    claude_dirs: Iterable[Path] | None = None,
    windsurf_inbox: Path | None = None,
) -> list[dict[str, object]]:
    """Report known local agent locations without reading conversations or writing state."""
    base = Path(home).expanduser() if home is not None else Path.home()
    canonical = Path(root).expanduser().resolve()
    claude_locations = list(claude_dirs) if claude_dirs is not None else [claude_project_dir(canonical, base)]
    codex = base / ".codex" / "sessions"
    devin = devin_database(base, data_home)
    hooks = canonical / ".windsurf" / "hooks.json"
    inbox = Path(windsurf_inbox).expanduser() if windsurf_inbox is not None else None
    antigravity = base / ".gemini" / "antigravity" / "conversations"
    return [
        {"source": "codex", "available": codex.is_dir(), "locations": [str(codex)]},
        {"source": "devin", "available": devin.is_file(), "locations": [str(devin)]},
        {"source": "claude", "available": any(path.is_dir() for path in claude_locations), "locations": [str(path) for path in claude_locations]},
        {"source": "antigravity", "available": antigravity.is_dir() and any(antigravity.glob("*.db")), "locations": [str(antigravity)]},
        {
            "source": "windsurf",
            "available": bool(inbox and inbox.is_dir() and any(inbox.glob("*.jsonl"))),
            "hook_configured": hooks.is_file(),
            "locations": [str(hooks), *([str(inbox)] if inbox is not None else [])],
        },
    ]
