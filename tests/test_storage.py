import tempfile
import stat
import sqlite3
import unittest
from pathlib import Path

from project_memory.models import NormalizedMessage
from project_memory.storage import MemoryRepository, VaultRepository


class StorageTests(unittest.TestCase):
    def test_knowledge_upsert_rejects_missing_or_ambiguous_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = MemoryRepository(Path(tmp) / "memory.sqlite3")
            message = NormalizedMessage("s1", "session-1", "duplicate", "project", "user", "t", "body", None, "h1", {})
            repo.upsert_message(message)

            with self.assertRaisesRegex(ValueError, "knowledge evidence"):
                repo.upsert_knowledge_item("missing", "project", "decision", "statement", "confirmed", "absent", "quote", "t")
            self.assertIsNone(repo.connection.execute("SELECT 1 FROM knowledge_items WHERE item_id='missing'").fetchone())

            repo.upsert_message(NormalizedMessage("s2", "session-2", "duplicate", "project", "user", "t", "body", None, "h2", {}))
            with self.assertRaisesRegex(ValueError, "knowledge evidence"):
                repo.upsert_knowledge_item("ambiguous", "project", "decision", "statement", "confirmed", "duplicate", "quote", "t")
            self.assertIsNone(repo.connection.execute("SELECT 1 FROM knowledge_items WHERE item_id='ambiguous'").fetchone())
            repo.close()

    def test_knowledge_upsert_rejects_explicit_nonexistent_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = MemoryRepository(Path(tmp) / "memory.sqlite3")
            with self.assertRaisesRegex(ValueError, "knowledge evidence"):
                repo.upsert_knowledge_item(
                    "wrong", "project", "decision", "statement", "confirmed", "message", "quote", "t",
                    evidence_source="s", evidence_session_id="session",
                )
            self.assertIsNone(repo.connection.execute("SELECT 1 FROM knowledge_items WHERE item_id='wrong'").fetchone())
            repo.close()

    def test_existing_task1_memory_database_is_migrated_additively(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version VALUES (1);
                CREATE TABLE messages(
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, project_id TEXT NOT NULL, role TEXT NOT NULL,
                    timestamp TEXT NOT NULL, content TEXT NOT NULL, source_path TEXT,
                    source_hash TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    UNIQUE(source, session_id, message_id));
                CREATE VIRTUAL TABLE messages_fts USING fts5(content, source, session_id, message_id, project_id);
                CREATE TABLE knowledge_items(
                    item_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
                    statement TEXT NOT NULL, state TEXT NOT NULL, evidence_message_id TEXT NOT NULL,
                    evidence_quote TEXT NOT NULL, timestamp TEXT NOT NULL);
                CREATE VIRTUAL TABLE knowledge_fts USING fts5(item_id UNINDEXED, project_id UNINDEXED, statement);
                CREATE TABLE embeddings(
                    object_type TEXT NOT NULL, object_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    model TEXT NOT NULL, dimension INTEGER NOT NULL, text_hash TEXT NOT NULL, vector BLOB NOT NULL,
                    PRIMARY KEY(object_type, object_id, model));
                CREATE TABLE embedding_models(model TEXT PRIMARY KEY, dimension INTEGER NOT NULL);
            """)
            conn.commit(); conn.close()
            repo = MemoryRepository(path)
            self.assertEqual(repo.connection.execute("SELECT version FROM schema_version").fetchone()[0], 3)
            columns = {row[1] for row in repo.connection.execute("PRAGMA table_info(messages)")}
            self.assertIn("object_key", columns)
            knowledge_columns = {row[1] for row in repo.connection.execute("PRAGMA table_info(knowledge_items)")}
            self.assertIn("evidence_source", knowledge_columns)
            repo.close()
    def test_memory_fts_match_stays_current_after_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = MemoryRepository(Path(tmp) / "nested" / "memory.sqlite3")
            message = NormalizedMessage(
                "hermes", "session", "message", "project", "user", "now",
                "initial searchable phrase", None, "hash", {},
            )
            repo.upsert_message(message)
            self.assertEqual(
                repo.connection.execute(
                    "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
                    ("initial",),
                ).fetchone()[0],
                1,
            )

            updated = NormalizedMessage(
                "hermes", "session", "message", "project", "user", "now",
                "updated searchable phrase", None, "hash", {},
            )
            repo.upsert_message(updated)
            self.assertEqual(
                repo.connection.execute(
                    "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
                    ("updated",),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                repo.connection.execute(
                    "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
                    ("initial",),
                ).fetchone()[0],
                0,
            )
            repo.close()

    def test_direct_repository_construction_secures_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault_parent = Path(tmp) / "insecure-vault-parent"
            vault_parent.mkdir(mode=0o755)
            vault = VaultRepository(vault_parent / "vault.sqlite3")
            self.assertEqual(stat.S_IMODE(vault_parent.stat().st_mode), 0o700)
            vault.close()

            memory_parent = Path(tmp) / "insecure-memory-parent"
            memory_parent.mkdir(mode=0o755)
            memory = MemoryRepository(memory_parent / "memory.sqlite3")
            self.assertEqual(stat.S_IMODE(memory_parent.stat().st_mode), 0o700)
            memory.close()

    def test_repositories_are_idempotent_and_separate_search_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault_path = root / "vault.sqlite3"
            memory_path = root / "memory.sqlite3"
            vault = VaultRepository(vault_path)
            memory = MemoryRepository(memory_path)
            vault.close()
            memory.close()
            vault = VaultRepository(vault_path)
            memory = MemoryRepository(memory_path)
            message = NormalizedMessage(
                source="hermes",
                session_id="session-1",
                message_id="message-1",
                project_id="project-1",
                role="user",
                timestamp="2026-08-27T00:00:00Z",
                content="redacted content",
                source_path="/tmp/export.jsonl",
                source_hash="source-hash",
                metadata={"z": 1, "a": "two"},
            )
            vault.upsert_message(message)
            vault.upsert_message(message)
            self.assertEqual(vault.count_messages(), 1)
            memory.upsert_message(message)
            memory.upsert_message(message)
            self.assertEqual(memory.count_messages(), 1)
            self.assertNotIn("raw_messages_fts", vault.table_names())
            self.assertIn("messages_fts", memory.table_names())
            self.assertIn("schema_version", vault.table_names())
            self.assertIn("schema_version", memory.table_names())
            vault.close()
            memory.close()

    def test_metadata_is_canonical_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vault.sqlite3"
            repo = VaultRepository(path)
            message = NormalizedMessage(
                "s", "sess", "msg", "p", "assistant", "t", "body", None, "h",
                {"z": 1, "a": 2},
            )
            repo.upsert_message(message)
            stored = repo.connection.execute(
                "SELECT metadata_json FROM messages"
            ).fetchone()[0]
            self.assertEqual(stored, '{"a": 2, "z": 1}')
            repo.close()


if __name__ == "__main__":
    unittest.main()
