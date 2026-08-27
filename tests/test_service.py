import json
import stat
import tempfile
import unittest
from pathlib import Path

from project_memory.adapters.base import SyncBatch
from project_memory.config import ProjectConfig, ProjectPaths
from project_memory.models import NormalizedMessage
from project_memory.redaction import Redactor
from project_memory.service import ProjectMemoryService


class FakeAdapter:
    source = "fake"
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0
    def scan(self, config, state):
        self.calls += 1
        return self.batches.pop(0) if self.batches else SyncBatch(0, (), state)


class UnavailableExtractor:
    def extract_turn(self, *args, **kwargs):
        from project_memory.extraction import ExtractionJob
        return ExtractionJob("pending", 1, "safe redacted prompt", error_code="model_unavailable")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.config = ProjectConfig.create("fixture", self.root)
        self.paths = ProjectPaths.for_root(self.root, Path(self.tmp.name) / "data")
        self.message = NormalizedMessage("fake", "session", "m1", self.config.project_id,
                                         "user", "2026-08-27", "token=CANARY secret decision", "/raw/source", "raw-hash", {})
        self.adapter = FakeAdapter([SyncBatch(1, (self.message,), {"cursor": 1})])
        self.service = ProjectMemoryService(
            self.config, self.paths, {"fake": self.adapter}, redactor=Redactor(("CANARY",)),
            extractor=UnavailableExtractor(), extraction_enabled=True,
        )
        self.addCleanup(self.service.close)
        self.addCleanup(self.tmp.cleanup)

    def test_first_and_no_change_sync_keep_raw_only_in_vault_and_pending_extraction(self):
        first = self.service.sync()
        second = self.service.sync()
        self.assertEqual(first["messages"], 1)
        self.assertEqual(second["messages"], 0)
        raw = self.service.vault.connection.execute("SELECT content FROM messages").fetchone()[0]
        self.assertIn("CANARY", raw)
        memory_bytes = self.paths.memory_db.read_bytes()
        self.assertNotIn(b"CANARY", memory_bytes)
        self.assertNotIn("CANARY", self.service.search("secret")[0].quote)
        self.assertEqual(self.service.memory.connection.execute("SELECT COUNT(*) FROM extraction_jobs WHERE status='pending'").fetchone()[0], 1)
        self.assertEqual(self.adapter.calls, 2)

    def test_failed_adapter_does_not_advance_checkpoint(self):
        class Failing:
            source = "broken"
            def scan(self, config, state):
                raise RuntimeError("secret adapter details")
        svc = ProjectMemoryService(self.config, self.paths, {"broken": Failing()})
        self.addCleanup(svc.close)
        result = svc.sync()
        self.assertTrue(result["warnings"])
        self.assertIsNone(svc.memory.connection.execute("SELECT 1 FROM sync_state WHERE source='broken'").fetchone())

    def test_project_isolation(self):
        other_root = Path(self.tmp.name) / "other"; other_root.mkdir()
        other = ProjectConfig.create("other", other_root)
        other_paths = ProjectPaths.for_root(other_root, Path(self.tmp.name) / "data")
        svc = ProjectMemoryService(other, other_paths, {})
        self.addCleanup(svc.close)
        self.assertEqual(svc.search("secret"), [])
        self.assertNotEqual(self.config.project_id, other.project_id)


if __name__ == "__main__":
    unittest.main()
