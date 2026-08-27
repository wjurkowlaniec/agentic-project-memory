from __future__ import annotations

import re
from dataclasses import dataclass, field

from .embeddings import EmbeddingIndex
from .redaction import Redactor
from .storage import MemoryRepository, message_object_key


@dataclass(frozen=True)
class SearchResult:
    object_type: str
    object_id: str
    score: float
    kind: str | None
    state: str | None
    role: str
    timestamp: str
    quote: str
    source: str
    session_id: str
    inspect_command: str
    metadata: dict[str, float | int | None] = field(default_factory=dict)


def _safe_fts_query(text: str) -> str:
    terms = re.findall(r"[\w]+", text, flags=re.UNICODE)
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


class HybridRetriever:
    def __init__(self, repository: MemoryRepository, embeddings: EmbeddingIndex, embedder, *, model: str, redactor=None):
        self.repository, self.embeddings, self.embedder, self.model = repository, embeddings, embedder, model
        self.redactor = redactor or Redactor()

    def search(self, project_id: str, query: str, limit: int = 10, *, include_history: bool = False) -> list[SearchResult]:
        if limit <= 0 or not query.strip():
            return []
        conn = self.repository.connection
        query = self.redactor.redact(query).text
        fts_query = _safe_fts_query(query)
        fts: dict[tuple[str, str], int] = {}
        if fts_query:
            fts_rows = conn.execute(
                "SELECT object_key FROM messages_fts WHERE project_id=? AND messages_fts MATCH ?",
                (project_id, fts_query),
            ).fetchall()
            for rank, row in enumerate(fts_rows, 1):
                fts[("message", row[0])] = rank
            knowledge_rows = conn.execute(
                "SELECT item_id FROM knowledge_fts WHERE project_id=? AND knowledge_fts MATCH ?",
                (project_id, fts_query),
            ).fetchall()
            for rank, row in enumerate(knowledge_rows, 1):
                fts[("knowledge", row[0])] = rank

        vector_rows = []
        if self.embedder is not None:
            try:
                query_vector = self.embedder.embed(self.model, [query])[0]
            except Exception:
                query_vector = None
            if query_vector is not None:
                vector_rows = self.embeddings.search(project_id, query_vector, limit=max(limit, 100))
        vector = {(row.object_type, row.object_id): rank for rank, row in enumerate(vector_rows, 1)}
        results = []
        for object_type, object_id in set(fts) | set(vector):
            if object_type == "message":
                row = conn.execute("SELECT * FROM messages WHERE object_key=? AND project_id=?", (object_id, project_id)).fetchone()
                if not row:
                    continue
                kind, state, role, timestamp, quote = None, None, row["role"], row["timestamp"], row["content"]
                source, session_id = row["source"], row["session_id"]
            else:
                row = conn.execute("SELECT * FROM knowledge_items WHERE item_id=? AND project_id=?", (object_id, project_id)).fetchone()
                if not row or row["state"] == "suppressed" or (not include_history and row["state"] in {"superseded", "rejected"}):
                    continue
                if row["evidence_source"] is None or row["evidence_session_id"] is None:
                    continue
                evidence = conn.execute(
                    "SELECT * FROM messages WHERE object_key=? AND project_id=?",
                    (message_object_key(row["evidence_source"], row["evidence_session_id"], row["evidence_message_id"]), project_id),
                ).fetchone()
                if not evidence:
                    continue
                kind, state, role, timestamp, quote = row["kind"], row["state"], evidence["role"], row["timestamp"], row["evidence_quote"]
                source, session_id = evidence["source"], evidence["session_id"]
            fts_rank, vector_rank = fts.get((object_type, object_id)), vector.get((object_type, object_id))
            rrf = (1 / (60 + fts_rank) if fts_rank else 0) + (1 / (60 + vector_rank) if vector_rank else 0)
            authority = 0.02 if state == "confirmed" and role == "user" else (-0.01 if role == "assistant" and state == "provisional" else 0)
            metadata = {"fts_rank": fts_rank, "vector_rank": vector_rank, "rrf_score": rrf, "authority_boost": authority}
            results.append(SearchResult(object_type, object_id, rrf + authority, kind, state, role, timestamp, quote, source, session_id, f"pmem inspect --root <project> {object_id}", metadata))
        return sorted(results, key=lambda result: (-result.score, result.object_type, result.object_id))[:limit]
