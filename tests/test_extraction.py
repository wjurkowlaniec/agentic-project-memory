import json
import unittest
from pathlib import Path

from project_memory.extraction import (
    ExtractionEngine,
    ExtractionError,
    parse_candidates,
    validate_candidate,
)
from project_memory.models import KnowledgeCandidate, NormalizedMessage


ROOT = Path(__file__).parent


def message(message_id="m-user", role="user", content="Please use at most 6 GB RAM."):
    return NormalizedMessage("synthetic", "s1", message_id, "p1", role, "now", content, None, "h", {})


class ExtractionTests(unittest.TestCase):
    def test_valid_direct_user_requirement_preserves_exact_offsets(self):
        source = message(content="Requirement max 6GB")
        candidate = KnowledgeCandidate("constraint", "Use at most 6 GB RAM", "confirmed", .95, "m-user", "max 6GB", 12, 19, True)
        result = validate_candidate(candidate, source, set(), trusted_authorization=True)
        self.assertEqual(result.state, "confirmed")
        self.assertEqual(source.content[result.evidence_start:result.evidence_end], result.evidence_quote)

    def test_model_direct_user_flag_is_provisional_without_trusted_authorization(self):
        candidate = KnowledgeCandidate("requirement", "Please use local inference", "confirmed", .9, "m-user", "Please", 0, 6, True)
        result = validate_candidate(candidate, message(content="Please use local inference"), set())
        self.assertEqual(result.state, "provisional")
        self.assertIn("trusted_authorization", " ".join(result.validation_warnings))

    def test_direct_user_candidate_stays_confirmed_only_with_trusted_authorization(self):
        candidate = KnowledgeCandidate("requirement", "Please use local inference", "confirmed", .9, "m-user", "Please", 0, 6, True)
        result = validate_candidate(candidate, message(content="Please use local inference"), set(), trusted_authorization=True)
        self.assertEqual(result.state, "confirmed")

    def test_agent_proposal_confirmed_is_downgraded_with_warning(self):
        candidate = KnowledgeCandidate("decision", "Use local inference", "confirmed", .9, "m-agent", "use local inference", 0, 19, False)
        result = validate_candidate(candidate, message("m-agent", "assistant", "use local inference"), set())
        self.assertEqual(result.state, "provisional")
        self.assertTrue(result.validation_warnings)

    def test_rejects_fabricated_quote_wrong_offset_empty_statement_invalid_state_confidence_and_unknown_target(self):
        cases = [
            KnowledgeCandidate("idea", "x", "provisional", .5, "m-user", "fabricated", 0, 9, True),
            KnowledgeCandidate("idea", "x", "provisional", .5, "m-user", "Please", 1, 7, True),
            KnowledgeCandidate("idea", "", "provisional", .5, "m-user", "Please", 0, 6, True),
            KnowledgeCandidate("idea", "x", "bogus", .5, "m-user", "Please", 0, 6, True),
            KnowledgeCandidate("idea", "x", "provisional", 1.1, "m-user", "Please", 0, 6, True),
            KnowledgeCandidate("idea", "x", "provisional", .5, "m-user", "Please", 0, 6, True, ("missing",), ()),
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ExtractionError):
                    validate_candidate(candidate, message(), set())

    def test_parser_accepts_zero_or_more_items_only_at_top_level_object(self):
        self.assertEqual(parse_candidates('{"items": []}'), [])
        valid = json.loads((ROOT / "fixtures/extraction_valid.json").read_text())
        self.assertEqual(parse_candidates(json.dumps(valid))[0].evidence_quote, "max 6GB")
        with self.assertRaises(ExtractionError):
            parse_candidates("[]")

    def test_invalid_span_fixture_is_rejected(self):
        payload = json.loads((ROOT / "fixtures/extraction_invalid_span.json").read_text())
        candidate = parse_candidates(payload)[0]
        with self.assertRaises(ExtractionError):
            validate_candidate(candidate, message(content="remote inference "), set())

    def test_engine_rejects_non_target_evidence_instead_of_dropping_it(self):
        class CandidateClient:
            def chat_json(self, *args, **kwargs):
                return {"items": [{"kind": "x", "statement": "x", "state": "provisional", "confidence": .5,
                    "evidence_message_id": "other", "evidence_quote": "x", "evidence_start": 0, "evidence_end": 1,
                    "direct_user_statement": False}]}
        result = ExtractionEngine(CandidateClient(), model="local").extract_turn(message(content="x"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "invalid_model_output")

    def test_engine_preserves_target_with_huge_preceding_neighbor(self):
        class FailingClient:
            def chat_json(self, *args, **kwargs):
                raise RuntimeError("secret prompt content")
        target = message(content="TARGET-CONTENT")
        engine = ExtractionEngine(FailingClient(), model="local", max_prompt_chars=500)
        prompt = engine._prompt(target, [message("prev", "assistant", "P" * 10000)])
        payload = json.loads(prompt.split("Messages:\n", 1)[1])
        self.assertEqual(payload[-1]["message_id"], target.message_id)
        self.assertEqual(payload[-1]["content"], target.content)
        self.assertLessEqual(len(prompt), 500)

    def test_engine_rejects_budget_too_small_for_instructions_and_target(self):
        engine = ExtractionEngine(object(), model="local", max_prompt_chars=100)
        with self.assertRaises(ExtractionError):
            engine._prompt(message(content="target"))

    def test_engine_bounds_context_and_leaves_failed_job_pending_with_sanitized_error(self):
        class FailingClient:
            def chat_json(self, *args, **kwargs):
                raise RuntimeError("secret prompt content")
        engine = ExtractionEngine(FailingClient(), model="local")
        result = engine.extract_turn(message(content="x" * 10000), [message("prev", "assistant", "previous")], [message("next", "user", "next")])
        self.assertEqual(result.status, "pending")
        self.assertEqual(result.error_code, "model_unavailable")
        self.assertNotIn("secret", str(result))
        self.assertLessEqual(len(result.prompt), engine.max_prompt_chars)
        self.assertEqual(result.attempts, 1)


if __name__ == "__main__":
    unittest.main()
