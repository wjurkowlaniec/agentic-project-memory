import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from project_memory.adapters.base import SyncBatch
from project_memory.config import ProjectConfig, ProjectPaths
from project_memory.extraction import ExtractionJob, ValidatedCandidate
from project_memory.models import KnowledgeCandidate, NormalizedMessage
from project_memory.redaction import Redactor
from project_memory.service import ProjectMemoryService
from project_memory.storage import MemoryRepository, message_object_key, parse_message_object_key


class Adapter:
    source = "src"
    def __init__(self, batches): self.batches = list(batches)
    def scan(self, config, state): return self.batches.pop(0) if self.batches else SyncBatch(0, (), state)


class Embedder:
    def __init__(self): self.calls = []
    def embed(self, model, texts):
        self.calls.append((model, texts))
        return [[1.0, 0.0] for _ in texts]


class Extractor:
    def __init__(self, jobs): self.jobs = list(jobs)
    def extract_turn(self, message): return self.jobs.pop(0)


def msg(project, mid="m1", text="Use SQLite", source_hash="h1", role="user"):
    return NormalizedMessage("src", "sess", mid, project, role, "2026-08-27T00:00:00Z", text, "/raw", source_hash, {"tool_body": "TOKEN=SECRET"})


class ReviewRound1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"; self.root.mkdir()
        self.config = ProjectConfig.create("p", self.root)
        self.paths = ProjectPaths.for_root(self.root, Path(self.tmp.name) / "data")
        self.addCleanup(self.tmp.cleanup)

    def service(self, adapter=None, **kwargs):
        svc = ProjectMemoryService(self.config, self.paths, {"src": adapter} if adapter else {},
                                   redactor=Redactor(("SECRET",)), embedding_dimension=2, **kwargs)
        self.addCleanup(svc.close); return svc

    def test_embedding_lifecycle_redacted_message_and_updated_source(self):
        e = Embedder(); first = msg(self.config.project_id, text="TOKEN=SECRET old", source_hash="h1")
        second = msg(self.config.project_id, text="TOKEN=SECRET new", source_hash="h2")
        svc = self.service(Adapter([SyncBatch(1, (first,), {"n": 1}), SyncBatch(1, (second,), {"n": 2})]), embedder=e)
        svc.sync(); svc.sync()
        row = svc.memory.connection.execute("SELECT text_hash FROM embeddings WHERE object_type='message'").fetchone()
        self.assertIsNotNone(row); self.assertEqual(len(e.calls), 2)
        self.assertTrue(all("TOKEN=SECRET" not in text for _, texts in e.calls for text in texts))
        self.assertEqual(svc.memory.connection.execute("SELECT content FROM messages").fetchone()[0], "TOKEN=[REDACTED:SECRET] new")
        self.assertEqual(svc.memory.connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 1)
        self.assertEqual(message_object_key("src", "sess", "m1"), svc.memory.connection.execute("SELECT object_key FROM messages").fetchone()[0])

    def test_embedding_failure_is_pending_and_retried_on_sync(self):
        class Flaky:
            def __init__(self): self.n = 0
            def embed(self, model, texts):
                self.n += 1
                if self.n == 1: raise RuntimeError("secret provider detail")
                return [[1.0, 0.0]]
        e = Flaky(); svc = self.service(Adapter([SyncBatch(1, (msg(self.config.project_id),), {"n": 1})]), embedder=e)
        svc.sync()
        self.assertEqual(svc.memory.connection.execute("SELECT status FROM embedding_jobs").fetchone()[0], "pending")
        svc.sync()
        self.assertEqual(svc.memory.connection.execute("SELECT status FROM embedding_jobs").fetchone()[0], "succeeded")
        self.assertEqual(svc.memory.connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 1)

    def test_correction_creates_trusted_raw_message_and_exact_redacted_evidence(self):
        svc = self.service()
        original = msg(self.config.project_id, text="Use SQLite")
        svc.vault.upsert_message(original)
        svc.memory.upsert_message(NormalizedMessage("src", "sess", "m1", self.config.project_id, "user", original.timestamp, original.content, original.source_path, original.source_hash, {"redaction_version": 1}))
        svc.memory.add_knowledge_candidate(self.config.project_id, "old", "decision", "Use SQLite", "confirmed", .9, "src", "sess", "m1", "Use SQLite", 0, 10, (), (), 1)
        result = svc.correct("old", "Use PostgreSQL TOKEN=SECRET", confirmed=True)
        self.assertEqual(result["state"], "confirmed")
        row = svc.memory.connection.execute("SELECT * FROM knowledge_items WHERE item_id=?", (result["item_id"],)).fetchone()
        self.assertEqual(row["statement"], row["evidence_quote"])
        self.assertEqual(row["statement"], "Use PostgreSQL TOKEN=[REDACTED:SECRET]")
        self.assertIsNotNone(svc.vault.connection.execute("SELECT 1 FROM messages WHERE source='pmem' AND session_id=? AND message_id=?", (result["session_id"], result["message_id"])).fetchone())
        self.assertNotIn(b"SECRET", svc.memory.path.read_bytes())
        self.assertEqual(tuple(svc.memory.connection.execute("SELECT source_id,target_id FROM item_links WHERE relation='supersedes'").fetchone()), (result["item_id"], "old"))

    def test_suppression_audit_rejects_blank_and_unknown(self):
        svc = self.service()
        with self.assertRaises(ValueError): svc.suppress("missing", "reason")
        with self.assertRaises(ValueError): svc.suppress("missing", "  ")
        svc.memory.upsert_message(NormalizedMessage("src", "sess", "m", self.config.project_id, "user", "t", "x", None, "h", {}))
        svc.memory.add_knowledge_candidate(self.config.project_id, "i", "fact", "x", "provisional", .5, "src", "sess", "m", "x", 0, 1, (), (), 1)
        svc.suppress("i", "not wanted")
        row = svc.memory.connection.execute("SELECT reason FROM suppression_log WHERE item_id='i'").fetchone()
        self.assertEqual(row[0], "not wanted")

    def test_preflight_syncs_before_search_and_does_not_call_unrelated_conflict(self):
        adapter = Adapter([SyncBatch(1, (msg(self.config.project_id, text="Deploy Friday"),), {"n": 1})])
        svc = self.service(adapter)
        result = svc.preflight("Friday")
        self.assertTrue(any(c["quote"] == "Deploy Friday" for c in result["citations"]))
        self.assertFalse(result["material_conflict"])

    def test_parse_key_and_latest_raw_revision(self):
        svc = self.service()
        svc.vault.upsert_message(msg(self.config.project_id, text="old", source_hash="a"))
        svc.vault.upsert_message(msg(self.config.project_id, text="new", source_hash="b"))
        key = message_object_key("src", "sess", "m1")
        self.assertEqual(parse_message_object_key(key), ("src", "sess", "m1"))
        self.assertEqual(svc.inspect(key, raw=True)["content"], "new")
        with self.assertRaises(ValueError): parse_message_object_key("message:not-valid")

    def test_schema_version_three_reopen_and_rebuild_failure_preserves_old(self):
        path = self.paths.memory_db
        repo = MemoryRepository(path); self.assertEqual(repo.connection.execute("SELECT version FROM schema_version").fetchone()[0], 3); repo.close()
        repo = MemoryRepository(path); self.assertEqual(repo.connection.execute("SELECT version FROM schema_version").fetchone()[0], 3); repo.close()
        svc = self.service(); svc.memory.upsert_message(NormalizedMessage("src", "sess", "m", self.config.project_id, "user", "t", "safe", None, "h", {}))
        old = path.read_bytes()
        svc.rebuild_failure_injector = lambda: (_ for _ in ()).throw(ValueError("injected"))
        with self.assertRaises(ValueError): svc.rebuild()
        self.assertEqual(svc.memory.connection.execute("SELECT content FROM messages WHERE message_id='m'").fetchone()[0], "safe")

    def test_status_inspect_redacted_and_rebuild_happy_path_preserves_knowledge(self):
        svc = self.service()
        raw = msg(self.config.project_id, text="TOKEN=SECRET decision")
        svc.vault.upsert_message(raw); derived = svc._redacted_message(raw); svc.memory.upsert_message(derived)
        svc.memory.add_knowledge_candidate(self.config.project_id, "keep", "fact", "decision", "confirmed", .8, "src", "sess", "m1", "decision", 0, 8, (), (), 1)
        self.assertEqual(svc.inspect(message_object_key("src", "sess", "m1"))["content"], "TOKEN=[REDACTED:SECRET] decision")
        self.assertEqual(svc.status()["messages"], 1)
        backup = svc.rebuild()
        self.assertTrue(Path(backup).exists())
        self.assertIsNotNone(svc.inspect("keep"))
        self.assertNotIn(b"TOKEN=SECRET", self.paths.memory_db.read_bytes())

    def test_successful_extraction_embeds_knowledge_and_updated_success_supersedes_old(self):
        candidate = KnowledgeCandidate("decision", "Use SQLite", "confirmed", .9, "m1", "Use SQLite", 0, 10, False)
        extractor = Extractor([ExtractionJob("succeeded", 1, "", (ValidatedCandidate(candidate),)), ExtractionJob("succeeded", 1, "", (ValidatedCandidate(KnowledgeCandidate("decision", "Use PostgreSQL", "provisional", .8, "m1", "Use PostgreSQL", 0, 14, False)),))])
        svc = self.service(Adapter([SyncBatch(1, (msg(self.config.project_id, text="Use SQLite", source_hash="a"),), {"n": 1}), SyncBatch(1, (msg(self.config.project_id, text="Use PostgreSQL", source_hash="b"),), {"n": 2})]), extractor=extractor, extraction_enabled=True, embedder=Embedder())
        svc.sync(); self.assertEqual(svc.memory.connection.execute("SELECT COUNT(*) FROM embeddings WHERE object_type='knowledge'").fetchone()[0], 1)
        svc.sync(); self.assertEqual(svc.memory.connection.execute("SELECT COUNT(*) FROM knowledge_items WHERE state='superseded'").fetchone()[0], 1)
    def test_receipt_db_failure_cleans_receipt_file(self):
        svc = self.service()
        svc.receipt_db_insert_hook = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("injected"))
        with self.assertRaises(sqlite3.OperationalError): svc.preflight("nothing")
        self.assertEqual(list(self.paths.receipts_dir.glob("*.json")), [])


if __name__ == "__main__": unittest.main()
