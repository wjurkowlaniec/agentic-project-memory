import math
import tempfile
import unittest
from pathlib import Path

from project_memory.storage import MemoryRepository
from project_memory.embeddings import EmbeddingError, EmbeddingIndex


class EmbeddingIndexTests(unittest.TestCase):
    def make_index(self, dimension=2):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = MemoryRepository(Path(self.tmp.name) / "memory.sqlite3")
        self.addCleanup(self.repo.close)
        self.addCleanup(self.tmp.cleanup)
        return EmbeddingIndex(self.repo, model="nomic", dimension=dimension)

    def test_normalizes_and_scores_identical_and_orthogonal_vectors(self):
        index = self.make_index()
        index.upsert("message", "m1", "p1", "same text", [3.0, 4.0])
        index.upsert("message", "m2", "p1", "other text", [0.0, 2.0])
        self.assertAlmostEqual(index.search("p1", [3.0, 4.0], limit=2)[0].score, 1.0)
        self.assertAlmostEqual(index.search("p1", [1.0, 0.0], limit=2)[1].score, 0.0, places=6)

    def test_rejects_zero_nan_wrong_dimension_and_model_mixing(self):
        index = self.make_index()
        for vector in ([0.0, 0.0], [math.nan, 1.0], [1.0]):
            with self.subTest(vector=vector):
                with self.assertRaises(EmbeddingError):
                    index.upsert("message", "bad", "p1", "text", vector)
        index.upsert("message", "m1", "p1", "text", [1.0, 0.0])
        with self.assertRaises(EmbeddingError):
            EmbeddingIndex(self.repo, model="nomic", dimension=3)

    def test_changed_text_hash_replaces_old_vector_and_search_is_project_scoped(self):
        index = self.make_index()
        index.upsert("message", "m1", "p1", "old text", [1.0, 0.0])
        index.upsert("message", "m1", "p1", "new text", [0.0, 1.0])
        index.upsert("message", "m2", "p2", "other project", [1.0, 0.0])
        rows = index.search("p1", [0.0, 1.0], limit=10)
        self.assertEqual([(row.object_type, row.object_id) for row in rows], [("message", "m1")])
        self.assertEqual(self.repo.connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 2)

    def test_embedding_primary_key_includes_project_id(self):
        index = self.make_index()
        index.upsert("message", "same-object", "p1", "p1 text", [1.0, 0.0])
        index.upsert("message", "same-object", "p2", "p2 text", [0.0, 1.0])
        self.assertEqual(len(index.search("p1", [1.0, 0.0])), 1)
        self.assertEqual(len(index.search("p2", [0.0, 1.0])), 1)
        self.assertEqual(self.repo.connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
