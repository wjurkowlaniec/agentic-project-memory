from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import tempfile
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .embeddings import EmbeddingIndex
from .extraction import ExtractionEngine
from .lmstudio import LMStudioClient
from .models import NormalizedMessage
from .redaction import Redactor
from .retrieval import HybridRetriever
from .storage import MemoryRepository, message_object_key, parse_message_object_key

QUALITY_GATE = {"candidate_precision": 0.90, "explicit_intent_recall": 0.80, "exact_evidence_validity": 1.0}
RETRIEVAL_GATE = {"retrieval_recall_at_5": 0.80}
STATEMENT_SIMILARITY_THRESHOLD = 0.80


class BenchmarkError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fixture:
    raw: dict[str, Any]
    redactor: Redactor

    @property
    def messages(self) -> list[dict[str, Any]]:
        return list(self.raw["messages"])

    @property
    def secret_canaries(self) -> tuple[str, ...]:
        return tuple(self.raw.get("secret_canaries", ()))

    def redacted_content(self, message: dict[str, Any]) -> str:
        return self.redactor.redact(message["content"]).text


def load_fixture(path: Path) -> Fixture:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise BenchmarkError("fixture_load_failed") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
        raise BenchmarkError("invalid_fixture")
    canaries = raw.get("secret_canaries", [])
    if not isinstance(canaries, list) or not all(isinstance(x, str) and x for x in canaries):
        raise BenchmarkError("invalid_fixture")
    redactor = Redactor(tuple(canaries))
    label_ids: set[str] = set()
    for message in raw["messages"]:
        if not isinstance(message, dict) or not isinstance(message.get("id"), str) or not isinstance(message.get("content"), str):
            raise BenchmarkError("invalid_fixture")
        labels = message.get("labels", [])
        if not isinstance(labels, list):
            raise BenchmarkError("invalid_fixture")
        redacted = redactor.redact(message["content"]).text
        for label in labels:
            required = {"label_id", "expected_kind", "statement", "evidence_quote", "evidence_start", "evidence_end"}
            if not isinstance(label, dict) or not required <= set(label) or not isinstance(label["label_id"], str) or label["label_id"] in label_ids:
                raise BenchmarkError("invalid_fixture_label")
            label_ids.add(label["label_id"])
            if not isinstance(label["expected_kind"], str) or not isinstance(label["statement"], str) or not isinstance(label["evidence_quote"], str):
                raise BenchmarkError("invalid_fixture_label")
            start, end = label["evidence_start"], label["evidence_end"]
            if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool) or start < 0 or end < start or redacted[start:end] != label["evidence_quote"]:
                raise BenchmarkError("invalid_fixture_span")
            if label.get("allow_bounded_overlap", False) not in (False, True):
                raise BenchmarkError("invalid_fixture_label")
    return Fixture(raw, redactor)


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[\w]+", text.casefold(), flags=re.UNICODE))


def _statement_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, " ".join(_tokens(left)), " ".join(_tokens(right))).ratio()


def evaluate_extraction(expected: list[dict[str, Any]], predicted: list[dict[str, Any]], sources: dict[str, str] | None = None) -> dict[str, float | int]:
    sources = sources or {}
    labels = {label["label_id"]: label for label in expected}
    direct_labels = {label_id for label_id, label in labels.items() if label.get("direct", True)}
    used: set[str] = set()
    matched = 0
    valid_evidence = 0
    for candidate in predicted:
        message_id = candidate.get("message_id")
        quote, start, end = candidate.get("quote"), candidate.get("start"), candidate.get("end")
        evidence_ok = isinstance(message_id, str) and isinstance(quote, str) and isinstance(start, int) and isinstance(end, int) and start >= 0 and end >= 0 and end >= start and message_id in sources and sources[message_id][start:end] == quote
        valid_evidence += int(evidence_ok)
        if not evidence_ok:
            continue
        for label_id, label in labels.items():
            if label_id in used or label["message_id"] != message_id or label["expected_kind"] != candidate.get("kind"):
                continue
            exact_span = (start, end, quote) == (label["evidence_start"], label["evidence_end"], label["evidence_quote"])
            overlap = bool(label.get("allow_bounded_overlap")) and start < label["evidence_end"] and end > label["evidence_start"]
            if (exact_span or overlap) and _statement_similarity(label["statement"], str(candidate.get("statement", ""))) >= STATEMENT_SIMILARITY_THRESHOLD:
                used.add(label_id)
                matched += 1
                break
    total = len(predicted)
    return {"matched_predictions": matched, "direct_labels": len(direct_labels), "candidate_precision": matched / total if total else 1.0, "explicit_intent_recall": len(used & direct_labels) / len(direct_labels) if direct_labels else 1.0, "exact_evidence_validity": valid_evidence / total if total else 1.0}


def evaluate_retrieval(cases: list[dict[str, Any]]) -> dict[str, float | int]:
    hits = sum(1 for case in cases if case.get("expected_label_id") in list(case.get("results", []))[:5])
    return {"retrieval_recall_at_5": hits / len(cases) if cases else 1.0, "retrieval_queries": len(cases)}


def gate_failures(metrics: dict[str, Any], gate: dict[str, float] = QUALITY_GATE) -> list[str]:
    return sorted(name for name, threshold in gate.items() if float(metrics.get(name, 0.0)) < threshold)


def write_report(directory: Path, payload: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    for _ in range(10):
        path = directory / f"benchmark-{stamp}-{secrets.token_hex(4)}.json"
        serial = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        temp = directory / f".{path.name}.tmp-{secrets.token_hex(4)}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            written = 0
            while written < len(serial):
                written += os.write(fd, serial[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temp, path)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        os.chmod(path, 0o600)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return path
    raise BenchmarkError("report_name_collision")


class BenchmarkRunner:
    def __init__(self, root: Path, fixture: Fixture, model: str, *, dry_run: bool = False, extraction_only: bool = False, retrieval_only: bool = False, allow_combined_models: bool = False, paths=None, client: Any = None, embedding_model: str | None = None):
        self.root, self.fixture, self.model = Path(root), fixture, model
        if extraction_only and retrieval_only:
            raise BenchmarkError("benchmark_modes_are_mutually_exclusive")
        if not dry_run and not extraction_only and not retrieval_only and not allow_combined_models:
            raise BenchmarkError("combined_models_require_explicit_opt_in")
        self.dry_run, self.extraction_only, self.retrieval_only = dry_run, extraction_only, retrieval_only
        self.allow_combined_models = allow_combined_models
        self.paths = paths or __import__("project_memory.config", fromlist=["ProjectPaths"]).ProjectPaths.for_root(self.root, self.root / ".project-memory")
        self.client = client
        self.embedding_model = embedding_model
        self.embedding_calls = 0
        self.extraction_calls = 0

    def _messages(self, project_id: str) -> list[NormalizedMessage]:
        return [NormalizedMessage("synthetic", "benchmark", item["id"], project_id, item.get("role", "user"), "2026-08-27T00:00:00Z", self.fixture.redacted_content(item), "synthetic", "fixture-" + item["id"], {"language": item.get("language"), "category": item.get("category")}) for item in self.fixture.messages]

    def _retrieval(self, project_id: str, messages: list[NormalizedMessage]) -> tuple[dict[str, Any], bool]:
        if self.dry_run or self.extraction_only:
            return {"retrieval_diagnostic": "not_run_in_dry_run", "retrieval_recall_at_5": None, "retrieval_queries": 0}, False
        if self.retrieval_only and (self.client is None or not self.embedding_model):
            raise BenchmarkError("embedding_model_required")
        if self.client is None or not self.embedding_model:
            return {"retrieval_diagnostic": "fts_only_not_quality"}, False
        self.paths.exports_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.paths.exports_dir.chmod(0o700)
        temp_dir = tempfile.TemporaryDirectory(prefix="benchmark-db-", dir=self.paths.exports_dir)
        benchmark_db = Path(temp_dir.name) / "memory.sqlite3"
        repo = MemoryRepository(benchmark_db)
        try:
            for message in messages:
                repo.upsert_message(message)
            vectors = self.client.embed(self.embedding_model, [message.content for message in messages])
            self.embedding_calls += 1
            if len(vectors) != len(messages) or not vectors or not isinstance(vectors[0], list):
                raise BenchmarkError("invalid_embedding_response")
            dimension = len(vectors[0])
            index = EmbeddingIndex(repo, model=self.embedding_model, dimension=dimension)
            for message, vector in zip(messages, vectors):
                index.upsert("message", message_object_key(message.source, message.session_id, message.message_id), project_id, message.content, vector)
            retriever = HybridRetriever(repo, index, self.client, model=self.embedding_model, redactor=self.fixture.redactor)
            retrieval_path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "retrieval_quality.json"
            queries = json.loads(retrieval_path.read_text(encoding="utf-8")).get("queries", [])
            labels_by_message = {item["id"]: [label["label_id"] for label in item.get("labels", [])] for item in self.fixture.messages}
            # One bulk call indexes the corpus; HybridRetriever makes one real
            # embedding call per query. Keep both in the auditable call count.
            self.embedding_calls += len(queries)
            cases = []
            for query in queries:
                results = retriever.search(project_id, query["query"], limit=5)
                ids = [parse_message_object_key(r.object_id)[2] for r in results if r.object_type == "message"]
                cases.append({"expected_label_id": query["expected_label_id"], "results": [label_id for message_id in ids for label_id in labels_by_message.get(message_id, [])]})
            return evaluate_retrieval(cases), True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise BenchmarkError("retrieval_failed") from None
        finally:
            repo.close()
            temp_dir.cleanup()

    def run(self) -> dict[str, Any]:
        started = time.perf_counter()
        project_id = "synthetic-benchmark"
        messages = self._messages(project_id)
        retrieval_metrics, embedding_mode = self._retrieval(project_id, messages)
        predicted: list[dict[str, Any]] = []
        extraction_elapsed = 0.0
        failed = pending = calls = 0
        if not self.retrieval_only:
            engine = None if self.dry_run else ExtractionEngine(self.client or LMStudioClient(), self.model)
            for message, source in zip(messages, self.fixture.messages):
                t0 = time.perf_counter()
                if self.dry_run:
                    for label in source.get("labels", []):
                        predicted.append({"message_id": message.message_id, "kind": label["expected_kind"], "statement": label["statement"], "quote": label["evidence_quote"], "start": label["evidence_start"], "end": label["evidence_end"]})
                else:
                    calls += 1
                    self.extraction_calls += 1
                    job = engine.extract_turn(message)
                    if job.status == "failed": failed += 1
                    if job.status == "pending":
                        pending += 1
                        raise BenchmarkError("model_unavailable")
                    predicted.extend({"message_id": c.evidence_message_id, "kind": c.kind, "statement": c.statement, "quote": c.evidence_quote, "start": c.evidence_start, "end": c.evidence_end} for c in job.candidates)
                extraction_elapsed += time.perf_counter() - t0
        expected = [{**label, "message_id": item["id"]} for item in self.fixture.messages for label in item.get("labels", [])]
        extraction_metrics = evaluate_extraction(expected, predicted, {m.message_id: m.content for m in messages}) if not self.retrieval_only else {}
        metrics = {**extraction_metrics, **retrieval_metrics, "extraction_latency_ms": extraction_elapsed * 1000, "total_runtime_ms": (time.perf_counter() - started) * 1000, "model_calls": calls + self.embedding_calls, "extraction_calls": self.extraction_calls, "embedding_calls": self.embedding_calls, "failed_rate": failed / len(messages) if messages else 0.0, "pending_rate": pending / len(messages) if messages else 0.0}
        if self.dry_run:
            gate = {}
        elif self.retrieval_only:
            gate = RETRIEVAL_GATE if embedding_mode else {}
        else:
            gate = {**QUALITY_GATE, **(RETRIEVAL_GATE if embedding_mode else {})}
        failures = gate_failures(metrics, gate) if gate else []
        quality_eligible = not self.dry_run and (not self.retrieval_only or embedding_mode)
        mode = "extraction-only" if self.extraction_only else ("retrieval-only" if self.retrieval_only else ("dry-run" if self.dry_run else "combined"))
        endpoint_scope = getattr(self.client, "endpoint_scope", "unknown")
        if endpoint_scope not in {"local_loopback", "remote_opt_in"}:
            endpoint_scope = "unknown"
        return {"schema_version": 2, "fixture": "synthetic", "metrics": metrics, "gate": gate, "gate_failures": failures, "quality_eligible": quality_eligible, "gate_passed": (not failures) if quality_eligible else None, "secret_canaries_leaked": sum(1 for canary in self.fixture.secret_canaries if any(canary in message.content for message in messages)), "runtime": {"mode": mode, "project_id": project_id, "model": self.model if not self.retrieval_only else None, "embedding_model": self.embedding_model if not self.extraction_only else None, "embedding_mode": embedding_mode, "endpoint_scope": endpoint_scope, "python": sys.version.split()[0], "platform": platform.platform(), "parallelism": 1, "context_length": 4096}}

    def run_and_write(self) -> Path:
        return write_report(self.paths.exports_dir, self.run())
