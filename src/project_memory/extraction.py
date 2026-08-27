from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from .models import KnowledgeCandidate, NormalizedMessage


VALID_STATES = {"provisional", "confirmed", "superseded", "rejected", "suppressed"}


class ExtractionError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedCandidate:
    candidate: KnowledgeCandidate
    validation_warnings: tuple[str, ...] = ()

    def __getattr__(self, name: str):
        return getattr(self.candidate, name)


@dataclass(frozen=True)
class ExtractionJob:
    status: str
    attempts: int
    prompt: str
    candidates: tuple[ValidatedCandidate, ...] = ()
    error_code: str | None = None


class _Client(Protocol):
    def chat_json(self, model: str, messages: list[dict[str, str]], temperature: float = 0.0) -> dict[str, object]: ...


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ExtractionError(f"invalid_{field}")
    return value


def parse_candidates(payload: str | dict[str, object]) -> list[KnowledgeCandidate]:
    try:
        value = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, ValueError) as exc:
        raise ExtractionError("invalid_json") from exc
    if not isinstance(value, dict) or set(value) != {"items"} or not isinstance(value["items"], list):
        raise ExtractionError("invalid_schema")
    result = []
    for item in value["items"]:
        if not isinstance(item, dict):
            raise ExtractionError("invalid_item")
        required = {"kind", "statement", "state", "confidence", "evidence_message_id", "evidence_quote", "evidence_start", "evidence_end", "direct_user_statement", "conflicts_with", "supersedes"}
        if set(item) != required:
            raise ExtractionError("invalid_item")
        try:
            candidate = KnowledgeCandidate(
                kind=_string(item["kind"], "kind"), statement=_string(item["statement"], "statement"),
                state=_string(item["state"], "state"), confidence=item["confidence"],
                evidence_message_id=_string(item["evidence_message_id"], "evidence_message_id"),
                evidence_quote=_string(item["evidence_quote"], "evidence_quote"),
                evidence_start=item["evidence_start"], evidence_end=item["evidence_end"],
                direct_user_statement=item["direct_user_statement"],
                conflicts_with=tuple(item["conflicts_with"]), supersedes=tuple(item["supersedes"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractionError("invalid_item") from exc
        if (not isinstance(candidate.direct_user_statement, bool)
                or not isinstance(candidate.confidence, (int, float)) or isinstance(candidate.confidence, bool)
                or not 0 <= candidate.confidence <= 1
                or not isinstance(candidate.evidence_start, int) or isinstance(candidate.evidence_start, bool)
                or not isinstance(candidate.evidence_end, int) or isinstance(candidate.evidence_end, bool)
                or not isinstance(item["conflicts_with"], list) or not isinstance(item["supersedes"], list)
                or not all(isinstance(x, str) for x in (*candidate.conflicts_with, *candidate.supersedes))):
            raise ExtractionError("invalid_item")
        result.append(candidate)
    return result


def validate_candidate(
    candidate: KnowledgeCandidate,
    message: NormalizedMessage,
    existing_item_ids: set[str],
    *,
    trusted_authorization: bool = False,
) -> ValidatedCandidate:
    if not candidate.kind or not candidate.statement or not candidate.evidence_quote:
        raise ExtractionError("empty_candidate")
    if candidate.state not in VALID_STATES:
        raise ExtractionError("invalid_state")
    if not isinstance(candidate.confidence, (int, float)) or isinstance(candidate.confidence, bool) or not 0 <= candidate.confidence <= 1:
        raise ExtractionError("invalid_confidence")
    if candidate.evidence_message_id != message.message_id:
        raise ExtractionError("unknown_evidence_message")
    if not isinstance(candidate.evidence_start, int) or not isinstance(candidate.evidence_end, int) or candidate.evidence_start < 0 or candidate.evidence_end < candidate.evidence_start or message.content[candidate.evidence_start:candidate.evidence_end] != candidate.evidence_quote:
        raise ExtractionError("invalid_evidence_span")
    if any(target not in existing_item_ids for target in (*candidate.conflicts_with, *candidate.supersedes)):
        raise ExtractionError("unknown_relation_target")
    warnings = []
    if not isinstance(trusted_authorization, bool):
        raise ExtractionError("invalid_trusted_authorization")
    if candidate.state == "confirmed" and (message.role != "user" or not candidate.direct_user_statement or not trusted_authorization):
        candidate = KnowledgeCandidate(candidate.kind, candidate.statement, "provisional", candidate.confidence, candidate.evidence_message_id, candidate.evidence_quote, candidate.evidence_start, candidate.evidence_end, candidate.direct_user_statement, candidate.conflicts_with, candidate.supersedes)
        warnings.append("confirmed_requires_trusted_authorization_and_direct_user_statement")
    return ValidatedCandidate(candidate, tuple(warnings))


PROMPT_INSTRUCTIONS = "Extract zero or more source-grounded knowledge items. kind is open; state is provisional, confirmed, superseded, rejected, or suppressed. Only an explicit direct user statement may be confirmed; agent proposals and inferences are provisional. Copy evidence_quote exactly, including whitespace, and return character offsets into the supplied redacted message. Return only {\"items\": [...]} with no extra keys."


class ExtractionEngine:
    def __init__(self, client: _Client, model: str, *, max_prompt_chars: int = 12000):
        self.client, self.model, self.max_prompt_chars = client, model, max_prompt_chars

    def _prompt(self, target: NormalizedMessage, preceding=(), following=()) -> str:
        before = next((m for m in reversed(tuple(preceding)) if m.role in {"user", "assistant"}), None)
        after = next((m for m in following if m.role in {"user", "assistant"}), None)
        prefix = PROMPT_INSTRUCTIONS + "\nMessages:\n"
        target_payload = {"message_id": target.message_id, "role": target.role, "content": target.content}
        target_text = json.dumps([target_payload], ensure_ascii=False)
        if len(prefix) + len(target_text) > self.max_prompt_chars:
            raise ExtractionError("prompt_budget_too_small")
        full_context = [m for m in (before, target, after) if m is not None]
        full_payload = [{"message_id": m.message_id, "role": m.role, "content": m.content} for m in full_context]
        full_text = prefix + json.dumps(full_payload, ensure_ascii=False)
        # Never cut JSON: if context does not fit, omit neighbors and retain target intact.
        return full_text if len(full_text) <= self.max_prompt_chars else prefix + target_text

    def extract_turn(self, target: NormalizedMessage, preceding=(), following=()) -> ExtractionJob:
        prompt = ""
        try:
            prompt = self._prompt(target, preceding, following)
            response = self.client.chat_json(self.model, [{"role": "user", "content": prompt}], temperature=0.0)
            parsed = parse_candidates(response)
            validated = tuple(validate_candidate(candidate, target, set(), trusted_authorization=False) for candidate in parsed)
            return ExtractionJob("succeeded", 1, prompt, validated)
        except ExtractionError:
            return ExtractionJob("failed", 1, prompt, error_code="invalid_model_output")
        except Exception:
            return ExtractionJob("pending", 1, prompt, error_code="model_unavailable")
