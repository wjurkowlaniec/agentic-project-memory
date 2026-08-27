import tempfile
import sqlite3
import unittest
from pathlib import Path

from project_memory.models import NormalizedMessage
from project_memory.storage import MemoryRepository
from project_memory.embeddings import EmbeddingIndex
from project_memory.retrieval import HybridRetriever, SearchResult
from project_memory.storage import message_object_key


def msg(message_id, project, role, content, timestamp="2026-08-27T00:00:00Z", session="session-1"):
    return NormalizedMessage("synthetic", session, message_id, project, role, timestamp, content, "fixture", "hash-" + message_id + session, {})


class FakeEmbedder:
    def embed(self, model, texts):
        return [[1.0, 0.0] for _ in texts]


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = MemoryRepository(Path(self.tmp.name) / "memory.sqlite3")
        self.addCleanup(self.repo.close)
        self.addCleanup(self.tmp.cleanup)
        self.index = EmbeddingIndex(self.repo, model="nomic", dimension=2)
        for message in (
            msg("m-user", "p1", "user", "Use SQLite for the memory index."),
            msg("m-agent", "p1", "assistant", "I propose a vector-only index."),
            msg("m-other", "p2", "user", "Use SQLite for another project."),
        ):
            self.repo.upsert_message(message)
        self.repo.upsert_knowledge_item("i-confirmed", "p1", "decision", "Use SQLite", "confirmed", "m-user", "Use SQLite", "2026-08-27T00:00:00Z")
        self.repo.upsert_knowledge_item("i-provisional", "p1", "idea", "Use vectors", "provisional", "m-agent", "vector-only", "2026-08-27T00:01:00Z")
        self.repo.upsert_knowledge_item("i-suppressed", "p1", "risk", "Hidden", "suppressed", "m-user", "SQLite", "2026-08-27T00:02:00Z")
        self.index.upsert("message", message_object_key("synthetic", "session-1", "m-user"), "p1", "Use SQLite for the memory index.", [1.0, 0.0])
        self.index.upsert("message", message_object_key("synthetic", "session-1", "m-agent"), "p1", "I propose a vector-only index.", [0.9, 0.1])
        self.index.upsert("message", message_object_key("synthetic", "session-1", "m-other"), "p2", "Use SQLite for another project.", [1.0, 0.0])
        self.index.upsert("knowledge", "i-confirmed", "p1", "Use SQLite", [1.0, 0.0])
        self.index.upsert("knowledge", "i-provisional", "p1", "Use vectors", [0.9, 0.1])

    def test_search_is_project_scoped_and_returns_auditable_evidence(self):
        results = HybridRetriever(self.repo, self.index, FakeEmbedder(), model="nomic").search("p1", "SQLite", limit=10)
        self.assertTrue(results)
        self.assertNotIn(message_object_key("synthetic", "session-1", "m-other"), {result.object_id for result in results})
        self.assertTrue(all(result.quote and result.source and result.session_id and result.inspect_command for result in results))
        self.assertTrue(all("fts_rank" in result.metadata and "vector_rank" in result.metadata for result in results))

    def test_rrf_and_authority_boost_confirmed_beats_provisional_and_suppressed_is_excluded(self):
        results = HybridRetriever(self.repo, self.index, FakeEmbedder(), model="nomic").search("p1", "SQLite", limit=10)
        ids = [result.object_id for result in results]
        self.assertEqual(ids[0], "i-confirmed")
        self.assertNotIn("i-suppressed", ids)
        self.assertGreater(results[0].metadata["rrf_score"], results[-1].metadata["rrf_score"])

    def test_optional_chronology_includes_superseded_and_rejected(self):
        self.repo.upsert_knowledge_item("i-old", "p1", "decision", "Old SQLite decision", "superseded", "m-user", "SQLite", "2026-08-26T00:00:00Z")
        results = HybridRetriever(self.repo, self.index, FakeEmbedder(), model="nomic").search("p1", "SQLite", limit=10, include_history=True)
        self.assertIn("i-old", {result.object_id for result in results})

    def test_fts_fallback_works_without_embeddings(self):
        results = HybridRetriever(self.repo, self.index, None, model="nomic").search("p1", "SQLite", limit=10)
        self.assertIn(message_object_key("synthetic", "session-1", "m-user"), {result.object_id for result in results})

    def test_same_message_id_in_different_sessions_returns_exact_messages(self):
        first = msg("same", "p1", "user", "alpha session content", session="session-a")
        second = msg("same", "p1", "user", "beta session content", session="session-b")
        self.repo.upsert_message(first)
        self.repo.upsert_message(second)
        results = HybridRetriever(self.repo, self.index, None, model="nomic").search("p1", "session", limit=10)
        by_session = {result.session_id: result for result in results}
        self.assertEqual(by_session["session-a"].quote, "alpha session content")
        self.assertEqual(by_session["session-b"].quote, "beta session content")

    def test_malformed_user_text_is_safe_in_fts_only_mode(self):
        retriever = HybridRetriever(self.repo, self.index, None, model="nomic")
        for query in ['"', 'OR NOT', 'foo:bar (baz)', '!!!']:
            with self.subTest(query=query):
                self.assertIsInstance(retriever.search("p1", query, limit=10), list)

    def test_knowledge_evidence_uses_source_and_session_identity(self):
        first = msg("same", "p1", "user", "first evidence", session="session-a")
        second = msg("same", "p1", "user", "second evidence", session="session-b")
        self.repo.upsert_message(first)
        self.repo.upsert_message(second)
        self.repo.upsert_knowledge_item("exact", "p1", "decision", "first", "confirmed", "same", "first evidence", "t", evidence_source="synthetic", evidence_session_id="session-a")
        result = HybridRetriever(self.repo, self.index, None, model="nomic").search("p1", "first", limit=10)
        self.assertEqual(next(item for item in result if item.object_id == "exact").session_id, "session-a")

    def test_migrated_unresolved_legacy_knowledge_is_skipped_during_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
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
                INSERT INTO messages VALUES
                    (1, 's1', 'session-1', 'duplicate', 'project', 'user', 't', 'body', NULL, 'h1', '{}'),
                    (2, 's2', 'session-2', 'duplicate', 'project', 'user', 't', 'body', NULL, 'h2', '{}');
                INSERT INTO knowledge_items VALUES
                    ('legacy-unresolved', 'project', 'decision', 'SQLite legacy', 'confirmed', 'duplicate', 'legacy', 't');
                INSERT INTO knowledge_fts(item_id, project_id, statement) VALUES ('legacy-unresolved', 'project', 'SQLite legacy');
            """)
            conn.commit()
            conn.close()
            repo = MemoryRepository(path)
            self.addCleanup(repo.close)
            legacy_index = EmbeddingIndex(repo, model="nomic", dimension=2)
            results = HybridRetriever(repo, legacy_index, None, model="nomic").search("project", "legacy", limit=10)
        self.assertNotIn("legacy-unresolved", {result.object_id for result in results})


if __name__ == "__main__":
    unittest.main()
