from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import NormalizedCommand
from .redaction import Redactor


_SENSITIVE_FLAG = re.compile(
    r"((?:--?(?:token|api[-_]?key|password|secret|credential)|-p)\s+)(\S+)", re.I,
)


def _sanitize(command: str, redactor: Redactor) -> str:
    compact = re.sub(r"\s+", " ", command).strip()
    return _SENSITIVE_FLAG.sub(
        r"\1[REDACTED:COMMAND_ARGUMENT]", redactor.redact(compact).text,
    )


class ProjectCommandIndex:
    """Commands explicitly recorded by agent conversations for one project."""

    def __init__(self, database: Path, *, redactor: Redactor | None = None):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database.parent.chmod(0o700)
        self.connection = sqlite3.connect(self.database)
        # These tables belonged to the removed global shell-history importer.
        # They contain generated, unscoped data and must not survive an upgrade.
        self.connection.execute("DROP TABLE IF EXISTS command_index")
        self.connection.execute("DROP TABLE IF EXISTS command_history_state")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS command_events (
                source TEXT NOT NULL,
                session_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                command TEXT NOT NULL,
                cwd TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                PRIMARY KEY (source, session_id, command_id, project_id)
            )
        """)
        self.connection.commit()
        self.database.chmod(0o600)
        self.redactor = redactor or Redactor()

    def close(self) -> None:
        self.connection.close()

    def upsert_commands(self, commands: Iterable[NormalizedCommand], project_id: str) -> dict[str, int]:
        changed = 0
        with self.connection:
            for item in commands:
                if item.project_id != project_id:
                    continue
                clean = _sanitize(item.command, self.redactor)
                if not clean:
                    continue
                before = self.connection.total_changes
                self.connection.execute("""
                    INSERT INTO command_events
                        (source, session_id, command_id, project_id, timestamp, command, cwd, source_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, session_id, command_id, project_id) DO UPDATE SET
                        timestamp=excluded.timestamp,
                        command=excluded.command,
                        cwd=excluded.cwd,
                        source_hash=excluded.source_hash
                    WHERE command_events.source_hash != excluded.source_hash
                       OR command_events.timestamp != excluded.timestamp
                """, (item.source, item.session_id, item.command_id, item.project_id,
                      item.timestamp, clean, item.cwd, item.source_hash))
                if self.connection.total_changes > before:
                    changed += 1
        unique = self.connection.execute(
            "SELECT COUNT(DISTINCT command) FROM command_events WHERE project_id=?", (project_id,),
        ).fetchone()[0]
        return {"events": changed, "commands": int(unique)}

    def list_commands(self, project_id: str, limit: int = 50) -> list[dict[str, object]]:
        rows = self.connection.execute("""
            SELECT command, COUNT(*), MIN(NULLIF(timestamp, '')), MAX(NULLIF(timestamp, ''))
            FROM command_events
            WHERE project_id=?
            GROUP BY command
            ORDER BY MAX(NULLIF(timestamp, '')) IS NULL,
                     MAX(NULLIF(timestamp, '')) DESC,
                     COUNT(*) DESC,
                     command
            LIMIT ?
        """, (project_id, max(1, limit))).fetchall()
        return [{
            "command": row[0], "count": row[1], "first_seen": row[2],
            "last_seen": row[3], "scope": "project-conversation",
        } for row in rows]
