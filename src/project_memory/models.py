from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedMessage:
    source: str
    session_id: str
    message_id: str
    project_id: str
    role: str
    timestamp: str
    content: str
    source_path: str | None
    source_hash: str
    metadata: dict[str, object]


@dataclass(frozen=True)
class NormalizedCommand:
    source: str
    session_id: str
    command_id: str
    project_id: str
    timestamp: str
    command: str
    cwd: str
    source_hash: str


@dataclass(frozen=True)
class KnowledgeCandidate:
    kind: str
    statement: str
    state: str
    confidence: float
    evidence_message_id: str
    evidence_quote: str
    evidence_start: int
    evidence_end: int
    direct_user_statement: bool
    conflicts_with: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
