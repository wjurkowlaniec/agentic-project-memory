import json
import stat
import tempfile
import unittest
from pathlib import Path

from project_memory.config import ProjectConfig, ProjectPaths
from project_memory.models import NormalizedMessage
from project_memory.service import ProjectMemoryService


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "project"; root.mkdir()
        config = ProjectConfig.create("fixture", root)
        paths = ProjectPaths.for_root(root, Path(self.tmp.name) / "data")
        self.service = ProjectMemoryService(config, paths, {})
        self.addCleanup(self.service.close); self.addCleanup(self.tmp.cleanup)
        for mid, text in (("m1", "Use SQLite for storage."), ("m2", "Use PostgreSQL for storage.")):
            self.service.vault.upsert_message(NormalizedMessage("src", "s", mid, config.project_id, "user", "t", text, None, mid, {}))
            self.service.memory.upsert_message(NormalizedMessage("src", "s", mid, config.project_id, "user", "t", text, None, mid, {}))
        self.service.memory.add_knowledge_candidate(config.project_id, "i1", "decision", "Use SQLite", "confirmed", .9, "src", "s", "m1", "Use SQLite", 0, 10, (), (), 1)
        self.service.memory.add_knowledge_candidate(config.project_id, "i2", "decision", "Use PostgreSQL", "confirmed", .9, "src", "s", "m2", "Use PostgreSQL", 0, 14, (), (), 1)

    def test_preflight_returns_exact_redacted_citations_conflict_and_atomic_receipt(self):
        result = self.service.preflight("storage")
        self.assertTrue(result["material_conflict"])
        self.assertTrue({"Use SQLite", "Use PostgreSQL"}.issubset({c["quote"] for c in result["citations"]}))
        self.assertEqual(result["conflict_ids"], ["i1", "i2"])
        self.assertEqual(self.service.memory.connection.execute("SELECT COUNT(*) FROM item_links").fetchone()[0], 0)
        receipt = Path(result["receipt_path"])
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        payload = json.loads(receipt.read_text())
        db = self.service.memory.connection.execute("SELECT receipt_json FROM preflight_receipts").fetchone()[0]
        self.assertEqual(payload, json.loads(db))
        self.assertEqual(payload["result_object_ids"][-2:], ["i1", "i2"])

    def test_correct_supersedes_only_after_user_confirmation_and_suppress_is_non_destructive(self):
        corrected = self.service.correct("i1", "Use PostgreSQL", confirmed=True)
        self.assertEqual(corrected["state"], "confirmed")
        self.assertEqual(self.service.memory.connection.execute("SELECT state FROM knowledge_items WHERE item_id='i1'").fetchone()[0], "superseded")
        self.assertEqual(tuple(self.service.memory.connection.execute("SELECT target_id FROM item_links WHERE source_id=? AND relation='supersedes'", (corrected["item_id"],)).fetchone()), ("i1",))
        self.service.suppress(corrected["item_id"], "user requested")
        self.assertEqual(self.service.memory.connection.execute("SELECT state FROM knowledge_items WHERE item_id=?", (corrected["item_id"],)).fetchone()[0], "suppressed")
        self.assertIsNotNone(self.service.memory.connection.execute("SELECT 1 FROM knowledge_items WHERE item_id='i1'").fetchone())


if __name__ == "__main__":
    unittest.main()
