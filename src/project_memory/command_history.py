from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .redaction import Redactor


_ZSH = re.compile(r"^:\s*(\d+):\d+;(.*)$")
_BASH_TIMESTAMP = re.compile(r"^#(\d+)$")
_SENSITIVE_FLAG = re.compile(r"((?:--?(?:token|api[-_]?key|password|secret|credential)|-p)\s+)(\S+)", re.I)


def _iso(timestamp: int | None) -> str | None:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else None


def _sanitize(command: str, redactor: Redactor) -> str:
    compact = re.sub(r"\s+", " ", command).strip()
    return _SENSITIVE_FLAG.sub(r"\1[REDACTED:COMMAND_ARGUMENT]", redactor.redact(compact).text)


class CommandHistoryIndex:
    """A local, aggregate-only snapshot of pre-existing shell history."""

    def __init__(self, database: Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database.parent.chmod(0o700)
        self.connection = sqlite3.connect(self.database)
        self.connection.execute("CREATE TABLE IF NOT EXISTS command_index (command TEXT PRIMARY KEY, count INTEGER NOT NULL, first_seen TEXT, last_seen TEXT, scope TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS command_history_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.connection.commit()
        self.database.chmod(0o600)

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def default_history_files(home: Path | None = None) -> list[Path]:
        base = Path(home).expanduser() if home is not None else Path.home()
        return [base / ".zsh_history", base / ".bash_history", base / ".local" / "share" / "fish" / "fish_history"]

    def _records(self, path: Path) -> Iterable[tuple[str, int | None]]:
        timestamp: int | None = None
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            zsh = _ZSH.match(raw)
            if zsh:
                yield zsh.group(2), int(zsh.group(1))
                timestamp = None
                continue
            bash = _BASH_TIMESTAMP.match(raw)
            if bash:
                timestamp = int(bash.group(1))
                continue
            if raw.strip() and not raw.startswith("- cmd:") and not raw.startswith("when:"):
                yield raw, timestamp
                timestamp = None

    def import_history(self, files: Iterable[Path] | None = None) -> dict[str, int | bool]:
        paths = [Path(path).expanduser() for path in (files if files is not None else self.default_history_files())]
        existing = [path for path in paths if path.is_file()]
        fingerprint = json.dumps([(str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in existing], separators=(",", ":"))
        row = self.connection.execute("SELECT value FROM command_history_state WHERE key='fingerprint'").fetchone()
        if row and row[0] == fingerprint:
            return {"changed": False, "commands": self.connection.execute("SELECT COUNT(*) FROM command_index").fetchone()[0], "records": 0}
        redactor = Redactor()
        aggregate: dict[str, dict[str, int | None]] = {}
        records = 0
        for path in existing:
            for command, timestamp in self._records(path):
                clean = _sanitize(command, redactor)
                if not clean:
                    continue
                records += 1
                entry = aggregate.setdefault(clean, {"count": 0, "first": None, "last": None})
                entry["count"] = int(entry["count"] or 0) + 1
                if timestamp is not None:
                    entry["first"] = timestamp if entry["first"] is None else min(int(entry["first"]), timestamp)
                    entry["last"] = timestamp if entry["last"] is None else max(int(entry["last"]), timestamp)
        with self.connection:
            self.connection.execute("DELETE FROM command_index")
            self.connection.executemany(
                "INSERT INTO command_index VALUES (?,?,?,?,?)",
                [(command, int(value["count"] or 0), _iso(value["first"]), _iso(value["last"]), "unscoped-history") for command, value in aggregate.items()],
            )
            self.connection.execute("INSERT INTO command_history_state VALUES ('fingerprint', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (fingerprint,))
        return {"changed": True, "commands": len(aggregate), "records": records}

    def list_commands(self, limit: int = 50) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT command,count,first_seen,last_seen,scope FROM command_index ORDER BY last_seen IS NULL, last_seen DESC, count DESC, command LIMIT ?", (max(1, limit),)
        ).fetchall()
        return [{"command": row[0], "count": row[1], "first_seen": row[2], "last_seen": row[3], "scope": row[4]} for row in rows]
