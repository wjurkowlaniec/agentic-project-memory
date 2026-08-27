from __future__ import annotations

import hashlib
import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from project_memory.adapters.devin import DevinAdapter
from project_memory.config import ProjectConfig


class DevinAdapterTests(unittest.TestCase):
    def make_database(self, directory: Path, root: Path, alias: Path) -> Path:
        path = directory / "devin.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                working_directory TEXT,
                backend_type TEXT,
                model TEXT,
                agent_mode TEXT,
                created_at TEXT,
                last_activity_at TEXT,
                title TEXT,
                main_chain_id TEXT,
                shell_last_seen_index INTEGER,
                cogs_json TEXT,
                workspace_dirs TEXT,
                hidden INTEGER,
                metadata TEXT
            );
            CREATE TABLE message_nodes (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                node_id INTEGER,
                parent_node_id INTEGER,
                chat_message TEXT,
                created_at INTEGER,
                metadata TEXT,
                UNIQUE(session_id, node_id)
            );
            """
        )
        sessions = [
            ("root", str(root / "nested" / ".."), "devin", "model", "default", "2026-08-27T10:00:00Z", "2026-08-27T10:01:00Z", "Root", "1", 0, "{}", "[]", 0, "{}"),
            ("alias", "/not-the-root", "devin", "model", "default", "2026-08-27T10:00:00Z", "2026-08-27T10:01:00Z", "Alias", "1", 0, "{}", json.dumps([str(alias)]), 0, "{}"),
            ("prefix", str(root) + "-extra", "devin", "model", "default", "2026-08-27T10:00:00Z", "2026-08-27T10:01:00Z", "Prefix", "1", 0, "{}", "[]", 0, "{}"),
            ("other", str(directory / "unrelated"), "devin", "model", "default", "2026-08-27T10:00:00Z", "2026-08-27T10:01:00Z", "Other", "1", 0, "{}", "[]", 0, "{}"),
        ]
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", sessions)
        nodes = [
            ("root", 101, None, json.dumps({"role": "user", "content": "Root request."}), 1_700_000_001, "{}"),
            ("root", 102, 101, json.dumps({"role": "assistant", "content": "Root answer.", "tool_calls": [
                {"id": "devin-exec", "function": {"name": "exec", "arguments": {"command": "git status"}}},
            ]}), 1_700_000_002, "{}"),
            ("root", 103, 102, json.dumps({"role": "tool", "content": "DO NOT IMPORT TOOL BODY"}), 1_700_000_003, "{}"),
            ("alias", 201, None, json.dumps({"role": "user", "content": "Alias request."}), 1_700_000_004, "{}"),
            ("prefix", 301, None, json.dumps({"role": "user", "content": "DO NOT IMPORT PREFIX"}), 1_700_000_005, "{}"),
            ("other", 401, None, json.dumps({"role": "assistant", "content": "DO NOT IMPORT OTHER", "tool_calls": [
                {"id": "other-exec", "function": {"name": "exec", "arguments": {"command": "cat secret"}}},
            ]}), 1_700_000_006, "{}"),
            ("root", 104, None, "{malformed chat json", 1_700_000_007, "{}"),
            ("root", 105, None, json.dumps({"role": "user", "content": ["DO NOT IMPORT MALFORMED SCHEMA"]}), 1_700_000_008, "{}"),
        ]
        db.executemany(
            "INSERT INTO message_nodes(session_id, node_id, parent_node_id, chat_message, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            nodes,
        )
        db.commit()
        db.close()
        return path

    def test_real_schema_matches_exact_root_or_workspace_alias_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            root = directory / "project"
            alias = directory / "alias"
            root.mkdir(); alias.mkdir()
            config = ProjectConfig.create("real-devin", root, [alias])
            database = self.make_database(directory, root, alias)
            before = database.read_bytes()

            first = DevinAdapter(database).scan(config, {})
            after = database.read_bytes()

        self.assertEqual(after, before)
        self.assertEqual(first.sessions_seen, 2)
        self.assertEqual([(m.session_id, m.message_id, m.role, m.timestamp, m.content) for m in first.messages], [
            ("root", "101", "user", "2023-11-14T22:13:21+00:00", "Root request."),
            ("root", "102", "assistant", "2023-11-14T22:13:22+00:00", "Root answer."),
            ("alias", "201", "user", "2023-11-14T22:13:24+00:00", "Alias request."),
        ])
        self.assertTrue(all(message.project_id == config.project_id for message in first.messages))
        self.assertEqual(first.messages[0].source_hash, hashlib.sha256(b"Root request.").hexdigest())
        self.assertNotIn("DO NOT IMPORT", " ".join(message.content for message in first.messages))
        self.assertTrue(all(message.source_path is None for message in first.messages))
        self.assertEqual(first.messages[0].source_hash, hashlib.sha256(b"Root request.").hexdigest())
        self.assertTrue(all(set(message.metadata) <= {"backend_type", "model", "agent_mode", "title"} for message in first.messages))
        self.assertEqual([(command.command_id, command.command) for command in first.commands], [("devin-exec", "git status")])
        self.assertEqual(first.commands[0].cwd, str(root.resolve()))

    def test_malformed_chat_json_or_schema_is_only_a_generic_batch_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            root = directory / "project"; root.mkdir()
            config = ProjectConfig.create("real-devin", root)
            result = DevinAdapter(self.make_database(directory, root, directory / "alias")).scan(config, {})

        self.assertEqual([message.message_id for message in result.messages], ["101", "102"])
        self.assertTrue(result.warnings)
        warning_text = " ".join(result.warnings).lower()
        self.assertNotIn("malformed chat json", warning_text)
        self.assertNotIn("do not import", warning_text)
        self.assertNotIn("chat_message", warning_text)
        self.assertNotIn("devin.sqlite3", warning_text)

    def test_second_scan_emits_no_messages_and_does_not_mutate_source_or_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            root = directory / "project"; root.mkdir()
            config = ProjectConfig.create("real-devin", root)
            database = self.make_database(directory, root, directory / "alias")
            adapter = DevinAdapter(database)
            first = adapter.scan(config, {})
            source_after_first = database.read_bytes()
            second = adapter.scan(config, first.next_state)
            source_after_second = database.read_bytes()

        self.assertEqual(second.messages, ())
        self.assertEqual(second.commands, ())
        self.assertEqual(second.sessions_seen, 0)
        self.assertEqual(second.next_state, first.next_state)
        self.assertEqual(source_after_second, source_after_first)


if __name__ == "__main__":
    unittest.main()
