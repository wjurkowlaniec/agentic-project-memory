import json
import os
import tempfile
import unittest
from pathlib import Path

from project_memory.benchmark import (
    QUALITY_GATE,
    BenchmarkError,
    BenchmarkRunner,
    evaluate_extraction,
    evaluate_retrieval,
    gate_failures,
    load_fixture,
    write_report,
)
from project_memory.extraction import parse_candidates


FIXTURE = Path(__file__).parent / "fixtures" / "extraction_quality.json"
RETRIEVAL = Path(__file__).parent / "fixtures" / "retrieval_quality.json"


class BenchmarkTests(unittest.TestCase):
    def test_fixture_is_bilingual_and_covers_at_least_thirty_cases_and_canaries(self):
        fixture = load_fixture(FIXTURE)
        self.assertGreaterEqual(len(fixture.messages), 30)
        languages = {message["language"] for message in fixture.messages}
        self.assertEqual(languages, {"pl", "en"})
        categories = {message["category"] for message in fixture.messages}
        self.assertTrue({"direct_idea", "explicit_requirement", "constraint", "rejection", "question", "agent_proposal", "negation", "supersession", "chatter", "secret_canary"} <= categories)
        self.assertGreaterEqual(len(fixture.secret_canaries), 3)
        body = json.dumps(fixture.raw, ensure_ascii=False)
        self.assertNotIn("@gmail.com", body)

    def test_every_label_has_unique_id_and_exact_redacted_span(self):
        fixture = load_fixture(FIXTURE)
        labels = [label for message in fixture.messages for label in message.get("labels", [])]
        self.assertEqual(len({label["label_id"] for label in labels}), len(labels))
        self.assertGreaterEqual(sum(len(message.get("labels", [])) > 1 for message in fixture.messages), 1)
        for message in fixture.messages:
            redacted = fixture.redacted_content(message)
            for label in message.get("labels", []):
                self.assertIn(label["expected_kind"], {"idea", "requirement", "constraint", "rejection", "decision"})
                self.assertEqual(label["evidence_quote"], redacted[label["evidence_start"]:label["evidence_end"]])

    def test_extraction_scoring_is_one_to_one_and_span_grounded(self):
        expected = [{"label_id": "a", "message_id": "m", "expected_kind": "idea", "statement": "Use local index", "evidence_quote": "local", "evidence_start": 0, "evidence_end": 5, "direct": True}]
        predicted = [{"message_id": "m", "kind": "idea", "statement": "Use local index", "quote": "local", "start": 0, "end": 5}, {"message_id": "m", "kind": "idea", "statement": "Use local index", "quote": "local", "start": 0, "end": 5}]
        result = evaluate_extraction(expected, predicted, {"m": "local index"})
        self.assertEqual(result["matched_predictions"], 1)
        self.assertEqual(result["candidate_precision"], 0.5)
        self.assertEqual(result["explicit_intent_recall"], 1.0)
        self.assertEqual(result["exact_evidence_validity"], 1.0)

    def test_dry_run_is_mechanics_only_and_ineligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = BenchmarkRunner(Path(tmp), load_fixture(FIXTURE), "dry-run-model", dry_run=True).run()
        self.assertFalse(result["quality_eligible"])
        self.assertIsNone(result["gate_passed"])
        self.assertEqual(result["runtime"]["mode"], "dry-run")

    def test_retrieval_only_requires_real_embedding_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BenchmarkError):
                BenchmarkRunner(Path(tmp), load_fixture(FIXTURE), "model", retrieval_only=True).run()

    def test_retrieval_only_embedding_run_uses_only_retrieval_gate(self):
        class Embedder:
            def __init__(self): self.calls = []
            def embed(self, model, texts):
                self.calls.append((model, len(texts)))
                if len(texts) > 1:
                    target_messages = {
                        "Pomysł: użyjmy lokalnego indeksu pamięci.",
                        "Requirement: reports must contain aggregate metrics only.",
                        "Constraint: use the real MemoryRepository for retrieval fixtures.",
                        "I reject storing raw prompts in benchmark reports.",
                        "Let's add deterministic ranking tie-breakers.",
                        "Keep working-set estimates below six gigabytes.",
                    }
                    return [[1.0, 0.0] if text in target_messages else [0.0, 1.0] for text in texts]
                query_vectors = {
                    "lokalny indeks pamięci": [1.0, 0.0],
                    "aggregate metrics only": [1.0, 0.0],
                    "rzeczywiste repozytorium do fixture retrieval": [1.0, 0.0],
                    "raw prompts benchmark reports": [1.0, 0.0],
                    "ranking deterministyczny": [1.0, 0.0],
                    "working-set below six gigabytes": [0.0, 1.0],
                }
                return [query_vectors[texts[0]]]
        with tempfile.TemporaryDirectory() as tmp:
            client = Embedder()
            result = BenchmarkRunner(Path(tmp), load_fixture(FIXTURE), "model", retrieval_only=True, client=client, embedding_model="local").run()
        self.assertEqual(len(client.calls), 7)
        self.assertEqual(client.calls[0], ("local", 31))
        self.assertEqual(result["metrics"]["embedding_calls"], 7)
        self.assertEqual(result["gate"], {"retrieval_recall_at_5": 0.80})
        self.assertEqual(result["gate_failures"], [])
        self.assertTrue(result["gate_passed"])

    def test_extraction_metrics_are_exact_and_gate_uses_ninety_eighty_hundred(self):
        result = evaluate_extraction(
            expected=[{"label_id": x, "message_id": "m", "expected_kind": "idea", "statement": x, "evidence_quote": x, "evidence_start": i, "evidence_end": i + 1, "direct": x in {"a", "b", "d"}} for i, x in enumerate("abcde")],
            predicted=[
                {"message_id": "m", "kind": "idea", "statement": x, "quote": x, "start": i, "end": i + 1} for i, x in enumerate("abc")
            ] + [{"message_id": "m", "kind": "idea", "statement": "x", "quote": "e", "start": 4, "end": 5}, {"message_id": "m", "kind": "idea", "statement": "bad", "quote": "bad", "start": 0, "end": 0}],
            sources={"m": "abcde"},
        )
        self.assertAlmostEqual(result["candidate_precision"], 3 / 5)
        self.assertAlmostEqual(result["explicit_intent_recall"], 2 / 3)
        self.assertAlmostEqual(result["exact_evidence_validity"], 4 / 5)
        self.assertIn("candidate_precision", gate_failures({**QUALITY_GATE, "candidate_precision": 0.89}))
        self.assertEqual(gate_failures({"candidate_precision": 0.90, "explicit_intent_recall": 0.80, "exact_evidence_validity": 1.0}), [])

    def test_retrieval_recall_at_five_is_computed(self):
        self.assertEqual(evaluate_retrieval([{"expected_label_id": "a", "results": ["z", "a"]}, {"expected_label_id": "b", "results": ["b"]}]), {"retrieval_recall_at_5": 1.0, "retrieval_queries": 2})
        self.assertEqual(evaluate_retrieval([{"expected_label_id": "a", "results": ["z"]}]), {"retrieval_recall_at_5": 0.0, "retrieval_queries": 1})

    def test_report_is_restrictive_timestamped_aggregate_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_report(Path(tmp), {"metrics": {"candidate_precision": 1.0}, "gate_passed": True, "fixture": "synthetic"})
            self.assertRegex(path.name, r"^benchmark-\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}\.json$")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text()
            self.assertNotIn("prompt", text.lower())
            self.assertNotIn("raw_output", text.lower())
            self.assertNotIn("fixture_body", text.lower())

    def test_dry_run_runner_uses_actual_repository_retrieval_and_returns_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = load_fixture(FIXTURE)
            result = BenchmarkRunner(root=root, fixture=fixture, model="dry-run-model", dry_run=True).run()
            self.assertEqual(result["runtime"]["mode"], "dry-run")
            self.assertIn("retrieval_recall_at_5", result["metrics"])
            self.assertIn("model", result["runtime"])
            self.assertEqual(result["secret_canaries_leaked"], 0)

    def test_runtime_reports_only_allowlisted_remote_scope(self):
        class RemoteClient:
            endpoint_scope = "remote_opt_in"
        with tempfile.TemporaryDirectory() as tmp:
            result = BenchmarkRunner(Path(tmp), load_fixture(FIXTURE), "model", dry_run=True, client=RemoteClient()).run()
        self.assertEqual(result["runtime"]["endpoint_scope"], "remote_opt_in")
        self.assertNotIn("https://", json.dumps(result))

    def test_runtime_reports_unknown_for_missing_or_unknown_endpoint_scope(self):
        class MissingScopeClient:
            pass
        class UnknownScopeClient:
            endpoint_scope = "unexpected"
        for client in (MissingScopeClient(), UnknownScopeClient()):
            with self.subTest(client=client.__class__.__name__), tempfile.TemporaryDirectory() as tmp:
                result = BenchmarkRunner(Path(tmp), load_fixture(FIXTURE), "model", dry_run=True, client=client).run()
            self.assertEqual(result["runtime"]["endpoint_scope"], "unknown")

    def test_retrieval_failure_suppresses_caught_cause(self):
        class BrokenEmbedder:
            def embed(self, *_args):
                raise ValueError("private retrieval details")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BenchmarkError) as raised:
                BenchmarkRunner(Path(tmp), load_fixture(FIXTURE), "model", retrieval_only=True, client=BrokenEmbedder(), embedding_model="local").run()
        self.assertEqual(raised.exception.args, ("retrieval_failed",))
        self.assertIsNone(raised.exception.__cause__)

    def test_invalid_fixture_is_execution_error(self):
        with self.assertRaises(BenchmarkError) as raised:
            load_fixture(Path("/does/not/exist"))
        self.assertEqual(str(raised.exception), "fixture_load_failed")
        self.assertIsNone(raised.exception.__cause__)

    def test_benchmark_never_changes_real_memory_database_or_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = type("Paths", (), {"project_id": "real-project", "memory_db": root / "memory.sqlite3", "exports_dir": root / "exports"})()
            from project_memory.storage import MemoryRepository
            repo = MemoryRepository(paths.memory_db)
            repo.connection.execute("INSERT INTO messages(source,session_id,message_id,project_id,role,timestamp,content,source_path,source_hash,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)", ("real", "s", "m", "real-project", "user", "now", "keep", None, "h", "{}"))
            repo.connection.commit(); repo.close()
            before = (paths.memory_db.read_bytes(), paths.memory_db.stat().st_size)
            result = BenchmarkRunner(root, load_fixture(FIXTURE), "model", dry_run=True, paths=paths).run()
            self.assertEqual(result["runtime"]["project_id"], "synthetic-benchmark")
            self.assertEqual((paths.memory_db.read_bytes(), paths.memory_db.stat().st_size), before)
            class Client:
                def embed(self, _model, texts): return [[1.0, 1.0] for _ in texts]
                def chat_json(self, *_args, **_kwargs): return {"items": []}
            BenchmarkRunner(root, load_fixture(FIXTURE), "model", extraction_only=True, paths=paths, client=Client()).run()
            BenchmarkRunner(root, load_fixture(FIXTURE), "model", retrieval_only=True, paths=paths, client=Client(), embedding_model="local").run()
            self.assertEqual((paths.memory_db.read_bytes(), paths.memory_db.stat().st_size), before)
            self.assertFalse(Path(str(paths.memory_db) + "-wal").exists())
            self.assertFalse(Path(str(paths.memory_db) + "-shm").exists())

    def test_modes_are_exclusive_and_combined_requires_explicit_opt_in(self):
        with self.assertRaises(BenchmarkError):
            BenchmarkRunner(Path(tempfile.mkdtemp()), load_fixture(FIXTURE), "model", extraction_only=True, retrieval_only=True)
        with self.assertRaises(BenchmarkError):
            BenchmarkRunner(Path(tempfile.mkdtemp()), load_fixture(FIXTURE), "model").run()

    def test_extraction_only_does_not_resolve_embedding_and_retrieval_only_does_not_extract(self):
        class Client:
            def __init__(self): self.embeds = 0; self.chats = 0
            def embed(self, *_): self.embeds += 1; return [[1.0, 1.0]] * 31
            def chat_json(self, *_args, **_kwargs): self.chats += 1; return {"items": []}
        with tempfile.TemporaryDirectory() as tmp:
            client = Client()
            result = BenchmarkRunner(Path(tmp), load_fixture(FIXTURE), "model", extraction_only=True, dry_run=True, client=client).run()
            self.assertEqual(client.embeds, 0); self.assertEqual(client.chats, 0)
            self.assertEqual(result["runtime"]["mode"], "extraction-only")
        with tempfile.TemporaryDirectory() as tmp:
            client = Client()
            result = BenchmarkRunner(Path(tmp), load_fixture(FIXTURE), "model", retrieval_only=True, client=client, embedding_model="local").run()
            self.assertEqual(client.chats, 0)
            self.assertEqual(result["metrics"]["extraction_calls"], 0)

    def test_pending_extraction_is_execution_error_and_writes_no_report(self):
        class UnavailableClient:
            def chat_json(self, *_args, **_kwargs):
                raise RuntimeError("private prompt and raw output")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = BenchmarkRunner(root, load_fixture(FIXTURE), "model", extraction_only=True, client=UnavailableClient())
            with self.assertRaises(BenchmarkError) as raised:
                runner.run_and_write()
            self.assertEqual(str(raised.exception), "model_unavailable")
            self.assertFalse(list((root / ".project-memory" / "exports").glob("benchmark-*.json")))
            self.assertNotIn("private", str(raised.exception))

    def test_invalid_completed_output_remains_quality_failure(self):
        class InvalidClient:
            def chat_json(self, *_args, **_kwargs):
                return {"not_items": []}

        with tempfile.TemporaryDirectory() as tmp:
            result = BenchmarkRunner(Path(tmp), load_fixture(FIXTURE), "model", extraction_only=True, client=InvalidClient()).run()
        self.assertTrue(result["quality_eligible"])
        self.assertFalse(result["gate_passed"])
        self.assertGreater(result["metrics"]["failed_rate"], 0)


if __name__ == "__main__":
    unittest.main()
