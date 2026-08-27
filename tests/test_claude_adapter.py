from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from project_memory.adapters.claude import ClaudeAdapter
from project_memory.config import ProjectConfig


class ClaudeAdapterTests(unittest.TestCase):
    def test_reads_only_user_and_assistant_messages_from_the_exact_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "project"; root.mkdir()
            folder = home / ".claude" / "projects" / "-tmp-project"; folder.mkdir(parents=True)
            transcript = folder / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(row) for row in [
                {"type": "user", "sessionId": "session", "cwd": str(root), "timestamp": "2026-08-27T12:00:00Z", "message": {"role": "user", "content": "Need local memory."}},
                {"type": "assistant", "sessionId": "session", "cwd": str(root), "timestamp": "2026-08-27T12:01:00Z", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Use project scope."},
                    {"type": "tool_use", "id": "claude-bash", "name": "Bash", "input": {"command": "git status"}},
                    {"type": "tool_use", "id": "claude-outside", "name": "Bash", "input": {"command": "cat secret", "workdir": str(Path(tmp) / "other")}},
                ]}},
                {"type": "tool", "sessionId": "session", "cwd": str(root), "message": {"role": "tool", "content": "DROP"}},
                {"type": "user", "sessionId": "other", "cwd": str(root) + "-other", "message": {"role": "user", "content": "DROP"}},
            ]) + "\n", encoding="utf-8")
            config = ProjectConfig.create("project", root)
            result = ClaudeAdapter(session_dirs=[folder]).scan(config, {})

        self.assertEqual([(item.message_id, item.role, item.content) for item in result.messages], [
            ("session:0", "user", "Need local memory."),
            ("session:1", "assistant", "Use project scope."),
        ])
        self.assertTrue(all(item.source == "claude" for item in result.messages))
        self.assertEqual([(command.command_id, command.command) for command in result.commands], [("claude-bash", "git status")])
        self.assertEqual(result.commands[0].cwd, str(root.resolve()))

    def test_second_scan_is_idempotent_and_malformed_records_only_warn_generically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"; root.mkdir()
            folder = Path(tmp) / "claude"; folder.mkdir()
            (folder / "session.jsonl").write_text('{bad\n', encoding="utf-8")
            adapter = ClaudeAdapter(session_dirs=[folder])
            first = adapter.scan(ProjectConfig.create("project", root), {})
            second = adapter.scan(ProjectConfig.create("project", root), first.next_state)

        self.assertTrue(first.warnings)
        self.assertEqual(second.messages, ())
        self.assertEqual(second.commands, ())
        self.assertEqual(second.next_state, first.next_state)


if __name__ == "__main__":
    unittest.main()
