from __future__ import annotations

import hashlib
import math
import sqlite3
from dataclasses import dataclass

import numpy

from .storage import MemoryRepository


class EmbeddingError(ValueError):
    pass


@dataclass(frozen=True)
class VectorResult:
    object_type: str
    object_id: str
    project_id: str
    score: float
    text_hash: str


class EmbeddingIndex:
    def __init__(self, repository: MemoryRepository, *, model: str, dimension: int):
        if not model or not isinstance(dimension, int) or dimension <= 0:
            raise EmbeddingError("invalid_embedding_configuration")
        self.repository, self.model, self.dimension = repository, model, dimension
        rows = repository.connection.execute(
            "SELECT dimension FROM embedding_models WHERE model=?", (model,)
        ).fetchall()
        if rows and any(row[0] != dimension for row in rows):
            raise EmbeddingError("model_dimension_mismatch")

    def _vector(self, values) -> numpy.ndarray:
        vector = numpy.asarray(values, dtype=numpy.float32)
        if vector.ndim != 1 or vector.size != self.dimension or not numpy.isfinite(vector).all():
            raise EmbeddingError("invalid_embedding_vector")
        norm = float(numpy.linalg.norm(vector))
        if not math.isfinite(norm) or norm == 0:
            raise EmbeddingError("invalid_embedding_vector")
        return vector / numpy.float32(norm)

    def upsert(self, object_type: str, object_id: str, project_id: str, text: str, vector) -> None:
        normalized = self._vector(vector)
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        conn = self.repository.connection
        conn.execute(
            "INSERT INTO embedding_models(model, dimension) VALUES (?, ?) "
            "ON CONFLICT(model) DO UPDATE SET dimension=excluded.dimension",
            (self.model, self.dimension),
        )
        conn.execute(
            """INSERT INTO embeddings
            (object_type, object_id, project_id, model, dimension, text_hash, vector)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(object_type, object_id, project_id, model) DO UPDATE SET
              project_id=excluded.project_id, dimension=excluded.dimension,
              text_hash=excluded.text_hash, vector=excluded.vector""",
            (object_type, object_id, project_id, self.model, self.dimension, text_hash,
             sqlite3.Binary(normalized.tobytes())),
        )
        conn.commit()

    def search(self, project_id: str, vector, *, limit: int = 10) -> list[VectorResult]:
        query = self._vector(vector)
        if limit <= 0:
            return []
        rows = self.repository.connection.execute(
            "SELECT object_type, object_id, project_id, dimension, text_hash, vector "
            "FROM embeddings WHERE project_id=? AND model=? AND dimension=?",
            (project_id, self.model, self.dimension),
        ).fetchall()
        if not rows:
            return []
        matrix = numpy.vstack([numpy.frombuffer(row[5], dtype=numpy.float32) for row in rows])
        if matrix.shape != (len(rows), self.dimension) or not numpy.isfinite(matrix).all():
            raise EmbeddingError("corrupt_embedding_vector")
        scores = query @ matrix.T
        scored = []
        for row, score in zip(rows, scores):
            scored.append(VectorResult(row[0], row[1], row[2], float(score), row[4]))
        return sorted(scored, key=lambda result: (-result.score, result.object_type, result.object_id))[:limit]
