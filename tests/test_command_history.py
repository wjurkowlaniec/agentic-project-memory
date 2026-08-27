from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from project_memory.command_history import CommandHistoryIndex


class CommandHistoryIndexTests(unittest.TestCase):
    def test_rebuilds_sanitized_history_with_count_and_last_seen_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / ".zsh_history"
            history.write_text(
                ": 1700000000:0;git status\n"
                ": 1700000010:0;curl --token top-secret https://example.test\n"
                ": 1700000020:0;git status\n"
                "npm test\n",
                encoding="utf-8",
            )
            index = CommandHistoryIndex(Path(tmp) / "commands.sqlite3")
            summary = index.import_history([history])
            rows = index.list_commands(limit=10)
            index.close()

        self.assertEqual(summary["commands"], 3)
        self.assertEqual(rows[0]["command"], "git status")
        self.assertEqual(rows[0]["count"], 2)
        self.assertEqual(rows[0]["last_seen"], "2023-11-14T22:13:40+00:00")
        self.assertIn("[REDACTED", rows[1]["command"])
        self.assertNotIn("top-secret", " ".join(row["command"] for row in rows))
        self.assertIsNone(rows[-1]["last_seen"])

    def test_reimport_rebuilds_snapshot_without_double_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / ".zsh_history"
            history.write_text(": 1700000000:0;git status\n", encoding="utf-8")
            index = CommandHistoryIndex(Path(tmp) / "commands.sqlite3")
            index.import_history([history])
            index.import_history([history])
            rows = index.list_commands(limit=10)
            index.close()

        self.assertEqual(rows, [{"command": "git status", "count": 1, "first_seen": "2023-11-14T22:13:20+00:00", "last_seen": "2023-11-14T22:13:20+00:00", "scope": "unscoped-history"}])
