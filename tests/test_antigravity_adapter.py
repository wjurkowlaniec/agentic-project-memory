from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_memory.adapters.antigravity import AntigravityAdapter
from project_memory.config import ProjectConfig


def _varint(value: int) -> bytes:
    result = bytearray()
    while value > 0x7f:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _metadata(project: Path) -> bytes:
    uri = project.resolve().as_uri().encode()
    workspace = _field(1, uri) + _field(2, uri)
    return _field(1, workspace) + _field(7, uri) + _field(18, b"synthetic")


class AntigravityAdapterTests(unittest.TestCase):
    def create_conversation(self, conversations: Path, brain: Path, conversation_id: str,
                            project: Path, rows: list[dict[str, object]]) -> Path:
        database = conversations / f"{conversation_id}.db"
        db = sqlite3.connect(database)
        db.execute("CREATE TABLE trajectory_metadata_blob (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        db.execute("INSERT INTO trajectory_metadata_blob VALUES (1, ?)", (_metadata(project),))
        db.commit(); db.close()
        logs = brain / conversation_id / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript_full.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8",
        )
        return database

    def test_imports_only_exact_project_user_and_planner_history_and_terminal_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            root = directory / "project"; root.mkdir()
            conversations = directory / "conversations"; conversations.mkdir()
            brain = directory / "brain"; brain.mkdir()
            rows = [
                {"step_index": 1, "type": "USER_INPUT", "status": "DONE", "created_at": "2026-08-27T10:00:00Z", "content": "Build project memory."},
                {"step_index": 2, "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-08-27T10:01:00Z", "content": "I will inspect it."},
                {"step_index": 3, "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-08-27T10:02:00Z", "tool_calls": [
                    {"name": "run_command", "args": {"command": "git status", "cwd": str(root / "frontend")}},
                    {"name": "run_command", "args": {"command": "cat secret", "cwd": str(directory / "other")}},
                ]},
                {"step_index": 4, "type": "VIEW_FILE", "status": "DONE", "created_at": "2026-08-27T10:03:00Z", "content": "DO NOT IMPORT TOOL OUTPUT"},
            ]
            database = self.create_conversation(conversations, brain, "matching", root, rows)
            before = database.read_bytes()
            self.create_conversation(conversations, brain, "prefix", Path(str(root) + "-other"), rows)
            config = ProjectConfig.create("project", root)

            first = AntigravityAdapter(conversations, brain).scan(config, {})
            second = AntigravityAdapter(conversations, brain).scan(config, first.next_state)
            after = database.read_bytes()

        self.assertEqual(after, before)
        self.assertEqual([(m.role, m.timestamp, m.content) for m in first.messages], [
            ("user", "2026-08-27T10:00:00Z", "Build project memory."),
            ("assistant", "2026-08-27T10:01:00Z", "I will inspect it."),
        ])
        self.assertTrue(all(m.session_id == "matching" and m.project_id == config.project_id for m in first.messages))
        self.assertNotIn("DO NOT IMPORT", " ".join(m.content for m in first.messages))
        self.assertEqual([(c.command, c.cwd) for c in first.commands], [("git status", str((root / "frontend").resolve()))])
        self.assertEqual(first.messages[0].source_hash, hashlib.sha256(b"Build project memory.").hexdigest())
        self.assertEqual(second.messages, ())
        self.assertEqual(second.commands, ())
        self.assertEqual(second.sessions_seen, 0)

    def test_missing_or_malformed_metadata_never_falls_back_to_text_heuristics(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            root = directory / "project"; root.mkdir()
            conversations = directory / "conversations"; conversations.mkdir()
            brain = directory / "brain"; brain.mkdir()
            database = conversations / "unscoped.db"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE trajectory_metadata_blob (id INTEGER PRIMARY KEY, data BLOB)")
            db.execute("INSERT INTO trajectory_metadata_blob VALUES (1, ?)", (b"mentions " + str(root).encode(),))
            db.commit(); db.close()
            logs = brain / "unscoped" / ".system_generated" / "logs"; logs.mkdir(parents=True)
            (logs / "transcript_full.jsonl").write_text(json.dumps({
                "step_index": 1, "type": "USER_INPUT", "status": "DONE",
                "created_at": "2026-08-27T10:00:00Z", "content": f"Work in {root}",
            }) + "\n")

            result = AntigravityAdapter(conversations, brain).scan(ProjectConfig.create("project", root), {})

        self.assertEqual(result.messages, ())
        self.assertEqual(result.commands, ())

    def test_default_scan_reads_both_antigravity_installation_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "project"; root.mkdir()
            for installation, conversation_id in (("antigravity", "classic"), ("antigravity-ide", "ide")):
                conversations = home / ".gemini" / installation / "conversations"
                conversations.mkdir(parents=True)
                brain = home / ".gemini" / installation / "brain"; brain.mkdir()
                self.create_conversation(conversations, brain, conversation_id, root, [{
                    "step_index": 1, "type": "USER_INPUT", "status": "DONE",
                    "created_at": "2026-08-27T10:00:00Z", "content": installation,
                }])
            with patch("project_memory.adapters.antigravity.Path.home", return_value=home):
                result = AntigravityAdapter().scan(ProjectConfig.create("project", root), {})

        self.assertEqual({message.session_id for message in result.messages}, {"classic", "ide"})
        self.assertEqual({message.content for message in result.messages}, {"antigravity", "antigravity-ide"})

    def test_backfills_pascal_case_antigravity_commands_without_reimporting_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            root = directory / "project"; root.mkdir()
            conversations = directory / "conversations"; conversations.mkdir()
            brain = directory / "brain"; brain.mkdir()
            self.create_conversation(conversations, brain, "session", root, [
                {"step_index": 1, "type": "USER_INPUT", "status": "DONE", "created_at": "2026-08-27T10:00:00Z", "content": "Already imported."},
                {"step_index": 2, "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-08-27T10:01:00Z", "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": "git status", "Cwd": str(root)}},
                ]},
            ])
            transcript = brain / "session" / ".system_generated" / "logs" / "transcript_full.jsonl"
            state = {"files": {str(transcript.resolve()): hashlib.sha256(transcript.read_bytes()).hexdigest()}}
            adapter = AntigravityAdapter(conversations, brain)

            first = adapter.scan(ProjectConfig.create("project", root), state)
            second = adapter.scan(ProjectConfig.create("project", root), first.next_state)

        self.assertEqual(first.messages, ())
        self.assertEqual([(command.command, command.cwd) for command in first.commands], [("git status", str(root.resolve()))])
        self.assertEqual(first.next_state["command_index_version"], 1)
        self.assertEqual(second.messages, ())
        self.assertEqual(second.commands, ())


if __name__ == "__main__":
    unittest.main()
