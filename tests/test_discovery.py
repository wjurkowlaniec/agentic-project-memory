from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from project_memory.discovery import discover_sources


class DiscoveryTests(unittest.TestCase):
    def test_discovers_only_known_local_agent_locations_without_reading_transcript_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "project"; root.mkdir()
            (home / ".codex" / "sessions").mkdir(parents=True)
            (home / ".local" / "share" / "devin" / "cli").mkdir(parents=True)
            (home / ".local" / "share" / "devin" / "cli" / "sessions.db").touch()
            (home / ".claude" / "projects" / "-tmp-project").mkdir(parents=True)
            (home / ".gemini" / "antigravity" / "conversations").mkdir(parents=True)
            (home / ".gemini" / "antigravity" / "conversations" / "conversation.db").touch()
            (root / ".windsurf").mkdir()
            (root / ".windsurf" / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
            found = discover_sources(root, home=home, claude_dirs=[home / ".claude" / "projects" / "-tmp-project"])

        by_source = {item["source"]: item for item in found}
        self.assertTrue(by_source["codex"]["available"])
        self.assertTrue(by_source["devin"]["available"])
        self.assertTrue(by_source["claude"]["available"])
        self.assertTrue(by_source["antigravity"]["available"])
        self.assertTrue(by_source["windsurf"]["hook_configured"])
        self.assertFalse(by_source["windsurf"]["available"])

    def test_missing_sources_are_reported_but_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "project"; root.mkdir()
            found = discover_sources(root, home=home)

        self.assertEqual({item["source"] for item in found}, {"codex", "devin", "claude", "windsurf", "antigravity"})
        self.assertFalse(any(item["available"] for item in found))

    def test_uses_xdg_data_home_for_devin_on_linux(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "project"; root.mkdir()
            data_home = Path(tmp) / "xdg"
            database = data_home / "devin" / "cli" / "sessions.db"
            database.parent.mkdir(parents=True); database.touch()
            found = discover_sources(root, home=home, data_home=data_home)

        devin = next(item for item in found if item["source"] == "devin")
        self.assertTrue(devin["available"])
        self.assertEqual(devin["locations"], [str(database)])


if __name__ == "__main__":
    unittest.main()
