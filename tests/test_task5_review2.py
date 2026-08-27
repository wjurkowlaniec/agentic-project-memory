import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from project_memory.adapters.base import SyncBatch
from project_memory.config import ProjectConfig, ProjectPaths
from project_memory.extraction import ExtractionJob
from project_memory.models import KnowledgeCandidate, NormalizedMessage
from project_memory.redaction import Redactor
from project_memory.service import ProjectMemoryService


class Adapter:
    source = "src"
    def __init__(self, batches): self.batches = list(batches)
    def scan(self, config, state): return self.batches.pop(0) if self.batches else SyncBatch(0, (), state)


class QueryEmbedder:
    def __init__(self): self.calls = []
    def embed(self, model, texts):
        self.calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]


class ConstantAttemptExtractor:
    def __init__(self, statuses): self.statuses = list(statuses)
    def extract_turn(self, message):
        return ExtractionJob(self.statuses.pop(0), 1, "", ())


def message(project, mid="m", text="Use SQLite", source_hash="h"):
    return NormalizedMessage("src", "sess", mid, project, "user", "2026-08-27T00:00:00Z", text, "/raw", source_hash, {})


class ReviewRound2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"; self.root.mkdir()
        self.config = ProjectConfig.create("p", self.root)
        self.paths = ProjectPaths.for_root(self.root, Path(self.tmp.name) / "data")
        self.addCleanup(self.tmp.cleanup)

    def service(self, adapter=None, **kwargs):
        svc = ProjectMemoryService(self.config, self.paths, {"src": adapter} if adapter else {},
                                   redactor=Redactor(("CANARY", "SECRET")), embedding_dimension=2, **kwargs)
        self.addCleanup(svc.close)
        return svc

    def add_item(self, svc, item_id, statement, *, kind="decision", state="confirmed", source="src", session="sess", mid):
        svc.memory.upsert_message(message(self.config.project_id, mid, statement, mid))
        svc.memory.add_knowledge_candidate(self.config.project_id, item_id, kind, statement, state, .9,
                                            source, session, mid, statement, 0, len(statement), (), (), 1)

    def test_suppression_reason_is_redacted_before_log_and_wal(self):
        svc = self.service()
        self.add_item(svc, "i", "Keep this", kind="fact", state="provisional", mid="m")
        svc.suppress("i", "because TOKEN=supersecret CANARY")
        reason = svc.memory.connection.execute("SELECT reason FROM suppression_log").fetchone()[0]
        self.assertEqual(reason, "because TOKEN=[REDACTED:SECRET] [REDACTED:CANARY]")
        self.assertNotIn(b"supersecret", svc.memory.path.read_bytes())
        self.assertNotIn(b"supersecret", svc.memory.path.with_name(svc.memory.path.name + "-wal").read_bytes())

    def test_search_and_preflight_redact_query_before_fts_embedding_and_receipt(self):
        embedder = QueryEmbedder()
        svc = self.service(Adapter([SyncBatch(1, (message(self.config.project_id, text="safe query"),), {})]), embedder=embedder)
        result = svc.preflight("safe TOKEN=SECRET CANARY")
        self.assertEqual(embedder.calls[-1], ["safe TOKEN=[REDACTED:SECRET] [REDACTED:CANARY]"])
        receipt = json.dumps(result, sort_keys=True)
        self.assertNotIn("SECRET", receipt)
        self.assertNotIn("safe TOKEN=SECRET", receipt)
        self.assertEqual(result["query_hash"], __import__("hashlib").sha256("safe TOKEN=[REDACTED:SECRET] [REDACTED:CANARY]".encode()).hexdigest())

    def test_receipt_renames_before_commit_and_failures_remove_row_and_file(self):
        for hook_name in ("receipt_after_rename_hook", "receipt_before_commit_hook"):
            with self.subTest(hook_name=hook_name):
                svc = self.service()
                setattr(svc, hook_name, lambda: (_ for _ in ()).throw(sqlite3.OperationalError("injected")))
                with self.assertRaises(sqlite3.OperationalError): svc.preflight("nothing")
                self.assertEqual(svc.memory.connection.execute("SELECT COUNT(*) FROM preflight_receipts").fetchone()[0], 0)
                self.assertEqual(list(self.paths.receipts_dir.glob("*.json")), [])

    def test_extraction_attempts_increment_from_persisted_attempts(self):
        extractor = ConstantAttemptExtractor(["pending", "pending", "succeeded"])
        svc = self.service(Adapter([SyncBatch(1, (message(self.config.project_id),), {}),
                                    SyncBatch(0, (), {}), SyncBatch(0, (), {})]), extractor=extractor, extraction_enabled=True)
        svc.sync(); self.assertEqual(svc.memory.connection.execute("SELECT attempts FROM extraction_jobs").fetchone()[0], 1)
        svc.sync(); self.assertEqual(svc.memory.connection.execute("SELECT attempts FROM extraction_jobs").fetchone()[0], 2)
        svc.sync(); row = svc.memory.connection.execute("SELECT status, attempts FROM extraction_jobs").fetchone()
        self.assertEqual(tuple(row), ("succeeded", 3))

    def test_knowledge_source_hash_join_is_project_scoped(self):
        svc = self.service()
        svc.memory.upsert_message(message("other", "m", "other", "other-h"))
        svc.memory.upsert_message(message(self.config.project_id, "m", "local", "local-h"))
        svc.memory.add_knowledge_candidate(self.config.project_id, "k", "fact", "local", "confirmed", .9, "src", "sess", "m", "local", 0, 5, (), (), 1)
        result = type("R", (), {"object_type": "knowledge", "object_id": "k"})()
        self.assertEqual(svc._source_hash(result), "local-h")

    def test_conflicts_require_explicit_link_or_narrow_competing_choice(self):
        svc = self.service()
        self.add_item(svc, "sqlite", "Use SQLite", mid="a")
        self.add_item(svc, "postgres", "Use PostgreSQL", mid="b")
        self.add_item(svc, "wal", "Use WAL with SQLite", mid="c")
        self.add_item(svc, "same", "Use SQLite", mid="d")
        self.add_item(svc, "arbitrary", "Use something else", mid="e")
        svc.memory.add_item_link("sqlite", "arbitrary", "conflicts_with", self.config.project_id, "t")
        result = svc.preflight("Use")
        self.assertEqual(result["conflict_ids"], ["arbitrary", "postgres", "same", "sqlite"])
        reasons = {(tuple(r["object_ids"]), r["reason"]) for r in result["conflict_reasons"]}
        self.assertEqual(len(reasons), len(result["conflict_reasons"]))
        self.assertNotIn(["sqlite", "wal"], [r["object_ids"] for r in result["conflict_reasons"]])
        self.assertNotIn(["arbitrary", "same"], [r["object_ids"] for r in result["conflict_reasons"]])

    def test_canary_marker_is_not_nested_and_existing_secret_redaction_remains(self):
        result = Redactor(("CANARY", "SECRET")).redact("CANARY TOKEN=SECRET")
        self.assertEqual(result.text, "[REDACTED:CANARY] TOKEN=[REDACTED:SECRET]")
        self.assertTrue(any(match.kind == "CANARY" for match in result.matches))


if __name__ == "__main__": unittest.main()
