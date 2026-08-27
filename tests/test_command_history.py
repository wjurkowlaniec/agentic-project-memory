from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from project_memory.command_history import ProjectCommandIndex
from project_memory.models import NormalizedCommand


class ProjectCommandIndexTests(unittest.TestCase):
    def command(self, command_id: str, command: str, timestamp: str) -> NormalizedCommand:
        return NormalizedCommand(
            source="codex", session_id="project-session", command_id=command_id,
            project_id="project-id", timestamp=timestamp, command=command,
            cwd="/synthetic/project", source_hash=f"hash-{command_id}",
        )

    def test_aggregates_only_explicit_project_command_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = ProjectCommandIndex(Path(tmp) / "commands.sqlite3")
            summary = index.upsert_commands([
                self.command("one", "git status", "2026-08-27T10:00:00Z"),
                self.command("two", "git status", "2026-08-27T11:00:00Z"),
                self.command("three", "curl --token top-secret https://example.test", "2026-08-27T10:30:00Z"),
            ], "project-id")
            rows = index.list_commands("project-id", limit=10)
            index.close()

        self.assertEqual(summary, {"events": 3, "commands": 2})
        self.assertEqual(rows[0]["command"], "git status")
        self.assertEqual(rows[0]["count"], 2)
        self.assertEqual(rows[0]["first_seen"], "2026-08-27T10:00:00Z")
        self.assertEqual(rows[0]["last_seen"], "2026-08-27T11:00:00Z")
        self.assertEqual(rows[0]["scope"], "project-conversation")
        self.assertNotIn("top-secret", " ".join(str(row["command"]) for row in rows))

    def test_reimport_is_idempotent_and_other_project_is_not_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = ProjectCommandIndex(Path(tmp) / "commands.sqlite3")
            event = self.command("one", "git status", "2026-08-27T10:00:00Z")
            index.upsert_commands([event], "project-id")
            index.upsert_commands([event], "project-id")
            other = NormalizedCommand("codex", "other-session", "two", "other-project",
                                      "2026-08-27T12:00:00Z", "npm test", "/other", "hash-two")
            index.upsert_commands([other], "other-project")
            rows = index.list_commands("project-id", limit=10)
            index.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 1)

    def test_has_no_shell_history_import_api(self):
        self.assertFalse(hasattr(ProjectCommandIndex, "import_history"))
        self.assertFalse(hasattr(ProjectCommandIndex, "default_history_files"))

    def test_opening_index_removes_legacy_unscoped_shell_history_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "commands.sqlite3"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE command_index (command TEXT PRIMARY KEY, count INTEGER, first_seen TEXT, last_seen TEXT, scope TEXT)")
            db.execute("CREATE TABLE command_history_state (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO command_index VALUES ('global command', 1, NULL, NULL, 'unscoped-history')")
            db.commit(); db.close()

            index = ProjectCommandIndex(database)
            tables = {row[0] for row in index.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            index.close()

        self.assertNotIn("command_index", tables)
        self.assertNotIn("command_history_state", tables)


if __name__ == "__main__":
    unittest.main()
