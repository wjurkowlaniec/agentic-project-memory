# Project Memory MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a verified local CLI that ingests SEP-related Codex and Hermes conversations, preserves a private raw copy, creates a redacted source-grounded project memory, and supplies audited preflight context to coding agents.

**Architecture:** Source adapters normalize messages into a restrictive raw vault and a separate redacted SQLite index. Local LM Studio models create provisional knowledge items with exact evidence spans; FTS5 and local embeddings provide hybrid project-scoped retrieval. A `pmem preflight` command synchronizes, retrieves cited context, and writes an audit receipt before the main agent plans; SEP remains a later downstream integration.

**Tech Stack:** Python 3.11+, standard library (`argparse`, `sqlite3`, `urllib.request`, `subprocess`, `unittest`), NumPy for float32 vector scoring, LM Studio OpenAI-compatible local API, Hermes CLI, SQLite FTS5.

**Spec:** `docs/superpowers/specs/2026-08-27-project-memory-mvp-design.md`

## Global Constraints

- Mutable history and databases must live under `~/.project-memory`, never inside a Git repository.
- Raw unredacted content is mode `0600`, is never embedded/indexed/sent to an LLM, and is printed only by explicit `inspect --raw`.
- Search/model-facing text is redacted before storage and processing.
- MVP knowledge is project-only; no cross-project/global fallback.
- Model-generated items default to `provisional`.
- Every item returned to an agent includes exact source evidence.
- The local model working set target is at most approximately 6 GB; models run sequentially with 4096 context and parallelism 1.
- Sync occurs through explicit commands at task start/end; no daemon or launchd job.
- No UI, MCP, cloud dependency, ChatGPT adapter, or Antigravity adapter.
- Do not commit, push, publish, or install a background service.
- Update `STATUS.md` and `docs/WORKLOG.md` at every completed task boundary.

## File Map

```text
pyproject.toml                         package metadata and pmem entry point
src/project_memory/__init__.py        package version
src/project_memory/__main__.py        python -m entry point
src/project_memory/models.py          shared immutable record types
src/project_memory/config.py          paths, project registration, permissions
src/project_memory/storage.py         vault/search schemas and repositories
src/project_memory/redaction.py       deterministic secret redaction
src/project_memory/adapters/base.py   adapter protocol and sync batch
src/project_memory/adapters/codex.py  Codex JSONL parser/checkpoints
src/project_memory/adapters/hermes.py Hermes CLI export parser/rolling sync
src/project_memory/lmstudio.py         local API and managed-model boundaries
src/project_memory/extraction.py       prompt, parsing, evidence validation
src/project_memory/embeddings.py       embedding persistence and cosine search
src/project_memory/retrieval.py        FTS, vector, RRF, authority/lifecycle rank
src/project_memory/service.py          sync, search, preflight, correction workflows
src/project_memory/rules.py            idempotent agent-rule installation
src/project_memory/cli.py              argparse command surface and rendering
tests/fixtures/                        synthetic Codex/Hermes/model fixtures
tests/test_*.py                        unit and integration coverage
docs/OPERATIONS.md                     install, commands, privacy, recovery
docs/BENCHMARK.md                      model/retrieval evaluation evidence
```

---

### Task 1: Package, project configuration, vault, search schema, and redaction

**Files:**
- Create: `pyproject.toml`
- Create: `src/project_memory/__init__.py`
- Create: `src/project_memory/models.py`
- Create: `src/project_memory/config.py`
- Create: `src/project_memory/storage.py`
- Create: `src/project_memory/redaction.py`
- Create: `tests/test_config.py`
- Create: `tests/test_storage.py`
- Create: `tests/test_redaction.py`
- Modify: `STATUS.md`
- Modify: `docs/WORKLOG.md`

**Interfaces:**
- Produces: `ProjectConfig`, `ProjectPaths`, `NormalizedMessage`, `KnowledgeCandidate`, `VaultRepository`, `MemoryRepository`, `Redactor`.
- Consumes: no earlier application code.

- [ ] **Step 1: Define package metadata and immutable records**

Create `pyproject.toml` with `requires-python = ">=3.11"`, dependency `numpy>=2.0`, package discovery under `src`, and script `pmem = "project_memory.cli:main"`.

Define records with these exact constructors:

```python
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
```

- [ ] **Step 2: Write failing configuration and permission tests**

Tests must create a temporary home and assert:

```python
paths = ProjectPaths.for_root(Path("/tmp/example"), data_home=temp_home)
self.assertEqual(paths.project_id, hashlib.sha256(b"/tmp/example").hexdigest()[:16])
config = ProjectConfig.create("example", Path("/tmp/example"), aliases=[Path("/tmp/alias")])
save_project_config(config, paths)
self.assertEqual(stat.S_IMODE(paths.config_file.stat().st_mode), 0o600)
self.assertEqual(load_project_config(Path("/tmp/alias"), data_home=temp_home).name, "example")
```

Run: `python3 -m unittest tests.test_config -v`
Expected: FAIL because the package does not exist.

- [ ] **Step 3: Implement configuration and restrictive path creation**

`ProjectPaths.for_root(root, data_home=None)` resolves the root, computes the stable ID, creates project directories with `0700`, and exposes `vault_db`, `memory_db`, `receipts_dir`, and `exports_dir`. `save_project_config` writes atomically via a sibling temporary file and `os.replace`, then enforces `0600`.

- [ ] **Step 4: Write failing schema/idempotency tests**

Tests must initialize both repositories twice, insert the same message twice, and assert one row. They must verify the search database contains FTS5 but the vault does not:

```python
vault.upsert_message(message)
vault.upsert_message(message)
self.assertEqual(vault.count_messages(), 1)
memory.upsert_message(redacted_message)
memory.upsert_message(redacted_message)
self.assertEqual(memory.count_messages(), 1)
self.assertNotIn("raw_messages_fts", vault.table_names())
self.assertIn("messages_fts", memory.table_names())
```

Run: `python3 -m unittest tests.test_storage -v`
Expected: FAIL with missing repository classes.

- [ ] **Step 5: Implement both SQLite schemas and repositories**

Use explicit migrations stored in `schema_version`. Enable foreign keys, WAL for the derived database, and a busy timeout. Use stable uniqueness keys `(source, session_id, message_id, source_hash)` in the vault and `(source, session_id, message_id)` in memory. Store metadata as canonical sorted JSON. After creating each database, enforce mode `0600`.

- [ ] **Step 6: Write redaction canary tests**

Cover PEM blocks, bearer/basic auth, credential URLs, common key formats, and secret-looking assignments. All `CANARY-*` values below are synthetic test markers, not credentials. Assert the literal canary is absent from returned text:

```python
samples = [
    "Authorization: Bearer sk-test-CANARY123456789",
    "DATABASE_PASSWORD=CANARY-password",
    "https://alice:CANARY-pass@example.test/path",
    "-----BEGIN PRIVATE KEY-----\nCANARY\n-----END PRIVATE KEY-----",
]
for sample in samples:
    redacted = Redactor().redact(sample)
    self.assertNotIn("CANARY", redacted.text)
    self.assertGreater(len(redacted.matches), 0)
```

Run: `python3 -m unittest tests.test_redaction -v`
Expected: FAIL with missing `Redactor`.

- [ ] **Step 7: Implement versioned redaction and run Task 1 tests**

Expose `REDACTION_VERSION = 1` and `Redactor.redact(text) -> RedactionResult`. Do not log input text. Preserve typed markers such as `[REDACTED:AUTH_TOKEN]`.

Run: `python3 -m unittest tests.test_config tests.test_storage tests.test_redaction -v`
Expected: PASS.

- [ ] **Step 8: Update durable status**

Record exact test output and created interfaces in `docs/WORKLOG.md`; mark Task 1 complete in `STATUS.md`. Run `git diff --check`.

---

### Task 2: Codex and Hermes source adapters

**Files:**
- Create: `.gitignore`
- Create: `src/project_memory/adapters/__init__.py`
- Create: `src/project_memory/adapters/base.py`
- Create: `src/project_memory/adapters/codex.py`
- Create: `src/project_memory/adapters/hermes.py`
- Create: `tests/fixtures/codex_session.jsonl`
- Create: `tests/fixtures/hermes_export.jsonl`
- Create: `tests/test_codex_adapter.py`
- Create: `tests/test_hermes_adapter.py`
- Modify: `STATUS.md`
- Modify: `docs/WORKLOG.md`

**Interfaces:**
- Consumes: `ProjectConfig`, `NormalizedMessage`, `VaultRepository`, `MemoryRepository`, `Redactor`.
- Produces: `SyncBatch`, `CodexAdapter.scan(config, state)`, `HermesAdapter.scan(config, state)`.

- [ ] **Step 1: Define adapter protocol and synthetic fixtures**

Create `.gitignore` entries for `.superpowers/`, `__pycache__/`, `*.py[cod]`, `*.egg-info/`, `.venv/`, `build/`, `dist/`, and any accidental local `.project-memory/` directory. This keeps review artifacts and generated Python files out of repository status without hiding source, tests, or documentation.

```python
@dataclass(frozen=True)
class SyncBatch:
    sessions_seen: int
    messages: tuple[NormalizedMessage, ...]
    next_state: dict[str, object]
    warnings: tuple[str, ...] = ()

class SourceAdapter(Protocol):
    def scan(self, config: ProjectConfig, state: dict[str, object]) -> SyncBatch: ...
```

Fixtures must include duplicate events, a partial final Codex line, alternate `session_meta` records, user/assistant/tool roles, a Hermes updated session, and a wrong-cwd session. Use synthetic content only.

- [ ] **Step 2: Write failing Codex parser/checkpoint tests**

Assert exact-cwd and alias matching, canonical response-message extraction, duplicate suppression, ignored reasoning, ignored partial final line, and next byte offset. Run `python3 -m unittest tests.test_codex_adapter -v`; expect failure.

- [ ] **Step 3: Implement Codex JSONL scanning**

Parse `session_meta.payload.cwd`; accept only canonical root/aliases. Prefer `response_item` messages to mirrored `event_msg` messages so one conversational message is imported once. Extract text from string content and `input_text`/`output_text` blocks. Store tool metadata without tool body in the derived message.

- [ ] **Step 4: Write failing Hermes exporter/parser tests**

Inject a fake command runner and assert the initial command includes:

```python
[
    "hermes", "sessions", "export", "--format", "jsonl",
    "--cwd", str(config.root), "--redact", "--yes", "-",
]
```

For incremental state, assert a bounded `--newer-than` rolling window is present. Verify user/assistant extraction, tool-body exclusion, updated-message replacement, and non-zero exporter exit handling.

- [ ] **Step 5: Implement Hermes rolling export adapter**

Use `subprocess.run(..., stdout=PIPE, stderr=PIPE, text=True, timeout=...)`. Never include exported content in errors. Parse one session object per JSONL line and stable message IDs from `messages[].id`. `--redact` output feeds memory; a second injectable exporter without `--redact` must stream unredacted records directly to the vault writer without a temporary plain-text file.

- [ ] **Step 6: Run adapter and idempotency tests**

Run: `python3 -m unittest tests.test_codex_adapter tests.test_hermes_adapter tests.test_storage -v`
Expected: PASS, including repeated imports with unchanged row counts.

- [ ] **Step 7: Update worklog/status and run `git diff --check`**

---

### Task 3: LM Studio transport, extraction schema, and exact evidence validation

**Files:**
- Create: `src/project_memory/lmstudio.py`
- Create: `src/project_memory/extraction.py`
- Create: `tests/fixtures/extraction_valid.json`
- Create: `tests/fixtures/extraction_invalid_span.json`
- Create: `tests/test_lmstudio.py`
- Create: `tests/test_extraction.py`
- Modify: `STATUS.md`
- Modify: `docs/WORKLOG.md`

**Interfaces:**
- Consumes: normalized redacted messages and repositories.
- Produces: `LMStudioClient`, `ExtractionEngine.extract_turn(...)`, `validate_candidate(...)`, pending/failed extraction job state.

- [ ] **Step 1: Write failing HTTP transport tests**

Use a local `http.server.ThreadingHTTPServer` fake. Assert `/v1/models`, `/v1/chat/completions`, and timeout/connection failure behavior. No test calls the real LM Studio server.

- [ ] **Step 2: Implement minimal OpenAI-compatible transport**

```python
class LMStudioClient:
    def __init__(self, base_url: str = "http://127.0.0.1:1234/v1", timeout: float = 60.0): ...
    def list_models(self) -> tuple[str, ...]: ...
    def chat_json(self, model: str, messages: list[dict[str, str]], temperature: float = 0.0) -> dict[str, object]: ...
    def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...
```

Reject non-local base URLs by default. Never put prompt bodies into exceptions.

- [ ] **Step 3: Write failing extraction/evidence tests**

Tests cover valid direct user requirement, agent proposal forced to provisional, fabricated quote, wrong offset, empty statement, invalid state, confidence out of range, and unknown relation target.

```python
candidate = KnowledgeCandidate(
    kind="constraint", statement="Use at most 6 GB RAM", state="confirmed",
    confidence=0.95, evidence_message_id=message.message_id,
    evidence_quote="max 6GB", evidence_start=12, evidence_end=19,
    direct_user_statement=True,
)
self.assertEqual(validate_candidate(candidate, message, existing_item_ids=set()).state, "confirmed")
```

- [ ] **Step 4: Implement strict prompt/parser/validator**

The prompt defines open `kind`, controlled state, role authority, exact quote copying, and zero-or-more JSON items. Parse only a top-level object `{"items": [...]}`. Strip no evidence whitespace silently; offsets and quote must match the redacted source exactly. Downgrade agent-authored or inferred `confirmed` output to `provisional` and record a validation warning.

- [ ] **Step 5: Implement bounded turn context and pending jobs**

Build prompts from one target message plus at most one preceding and one following user/assistant message, with a hard character budget derived from the 4096-token context. Failed/unavailable model calls leave jobs pending with attempt count and sanitized error code.

- [ ] **Step 6: Run Task 3 tests and update status**

Run: `python3 -m unittest tests.test_lmstudio tests.test_extraction -v`
Expected: PASS. Then run all tests and `git diff --check`.

---

### Task 4: Embeddings and project-scoped hybrid retrieval

**Files:**
- Create: `src/project_memory/embeddings.py`
- Create: `src/project_memory/retrieval.py`
- Create: `tests/test_embeddings.py`
- Create: `tests/test_retrieval.py`
- Modify: `STATUS.md`
- Modify: `docs/WORKLOG.md`

**Interfaces:**
- Consumes: `LMStudioClient.embed`, redacted messages/items, `MemoryRepository`.
- Produces: `EmbeddingIndex.upsert/search`, `SearchResult`, `HybridRetriever.search(project_id, query, limit)`.

- [ ] **Step 1: Write failing embedding serialization/cosine tests**

Store normalized float32 vectors as BLOBs with dimension/model/text hash. Assert identical vectors score `1.0`, orthogonal vectors score `0.0`, stale text hashes are replaced, and results never cross project IDs.

- [ ] **Step 2: Implement NumPy vector persistence/search**

Use `numpy.asarray(vector, dtype=numpy.float32)`, reject zero/NaN/wrong-dimension vectors, normalize before storage, and perform batched matrix multiplication on only the current project's vectors. Do not load raw-vault text.

- [ ] **Step 3: Write failing FTS/RRF/authority tests**

Construct deterministic fixtures where FTS and vector rankings differ. Assert reciprocal-rank fusion, confirmed explicit-user boost, provisional-agent demotion, suppressed exclusion, and optional chronology inclusion of superseded/rejected records.

- [ ] **Step 4: Implement hybrid retrieval**

```python
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
```

Use RRF constant `k=60`. Keep score components available in metadata for diagnosis. Search knowledge items and source messages so fallback mode remains useful without extraction.

- [ ] **Step 5: Run retrieval tests and all prior tests**

Run: `python3 -m unittest tests.test_embeddings tests.test_retrieval -v` and then `python3 -m unittest discover -s tests -v`. Expected: PASS.

- [ ] **Step 6: Update worklog/status and run `git diff --check`**

---

### Task 5: Application service, sync orchestration, conflict handling, corrections, and receipts

**Files:**
- Create: `src/project_memory/service.py`
- Create: `tests/test_service.py`
- Create: `tests/test_preflight.py`
- Modify: `STATUS.md`
- Modify: `docs/WORKLOG.md`

**Interfaces:**
- Consumes: adapters, repositories, redactor, extraction engine, embedding index, hybrid retriever.
- Produces: `ProjectMemoryService.init/sync/search/preflight/inspect/suppress/correct/rebuild/status`.

- [ ] **Step 1: Write failing end-to-end service tests with fakes**

Cover first sync, no-change sync, updated source, LM Studio unavailable with pending extraction, successful retry, exact citations, and project isolation. Assert raw and redacted contents differ for a canary secret and the canary never appears in memory database bytes.

- [ ] **Step 2: Implement transactional sync pipeline**

For each batch: write exact messages to vault, redact in memory, update FTS, enqueue embeddings/extractions, then checkpoint adapter state only after committed rows. Adapter failure must not erase prior state. Process tool bodies only into vault.

- [ ] **Step 3: Write failing conflict/correction tests**

Create two source-backed items with opposing statements and assert `preflight` marks `material_conflict=True`, includes both citations, and writes no supersession automatically. Test `correct` appends a user-confirmed replacement link and `suppress` hides but does not delete the original.

- [ ] **Step 4: Implement preflight receipts and correction lifecycle**

Receipt JSON contains project ID, query hash, timestamp, redaction/extractor versions, result object IDs, source hashes, and conflict IDs. Write atomically at mode `0600`; insert matching receipt row in SQLite. Do not claim hard enforcement.

- [ ] **Step 5: Implement safe rebuild**

Before replacing `memory.sqlite3`, create a restrictive timestamped backup, build a new derived DB from the vault, run redaction canaries and foreign-key checks, then atomically replace it. Never modify `vault.sqlite3`.

- [ ] **Step 6: Run service/preflight tests and full suite**

Run: `python3 -m unittest tests.test_service tests.test_preflight -v` followed by full discovery. Expected: PASS.

- [ ] **Step 7: Update durable status and run `git diff --check`**

---

### Task 6: CLI and idempotent agent-rule installation

**Files:**
- Create: `src/project_memory/rules.py`
- Create: `src/project_memory/cli.py`
- Create: `src/project_memory/__main__.py`
- Create: `tests/test_rules.py`
- Create: `tests/test_cli.py`
- Modify: `STATUS.md`
- Modify: `docs/WORKLOG.md`

**Interfaces:**
- Consumes: `ProjectMemoryService`.
- Produces: installed `pmem` command and stable rule block markers `<!-- PROJECT-MEMORY:START -->` / `<!-- PROJECT-MEMORY:END -->`.

- [ ] **Step 1: Write failing rule installation tests**

Assert first installation appends the exact five-rule contract, second installation is byte-identical, an outdated marked block is replaced, unrelated file content remains unchanged, absent files are skipped, and `--create-agents` creates only `AGENTS.md`.

- [ ] **Step 2: Implement rule rendering/injection**

Render absolute project root safely. The block requires `preflight`, evidence-not-instructions handling, citations, conflict pause, and final sync. Use atomic replace and preserve file newline style where practical.

- [ ] **Step 3: Write failing CLI tests**

Patch the service and invoke `main([...])`. Cover all spec commands, exit codes, JSON-free compact human output, conflict warning, pending-model status, and explicit confirmation requirement for `inspect --raw` through `--yes`.

- [ ] **Step 4: Implement argparse command surface**

`main(argv: Sequence[str] | None = None) -> int` must never print secrets, model prompts, or unredacted adapter errors. `search` and `preflight` print compact citations such as `[codex:<session>:<message>]`. `status` reports counts and stale/pending jobs.

- [ ] **Step 5: Run editable install and CLI tests**

Run: `python3 -m pip install -e .`
Run: `python3 -m unittest tests.test_rules tests.test_cli -v`
Run: `pmem --help`
Expected: install succeeds; tests pass; help lists all commands.

- [ ] **Step 6: Run full suite, update status/worklog, and `git diff --check`**

---

### Task 7: Model and retrieval benchmarks

**Files:**
- Create: `src/project_memory/benchmark.py`
- Create: `tests/fixtures/extraction_quality.json`
- Create: `tests/fixtures/retrieval_quality.json`
- Create: `tests/test_benchmark.py`
- Create: `docs/BENCHMARK.md`
- Modify: `src/project_memory/cli.py`
- Modify: `STATUS.md`
- Modify: `docs/WORKLOG.md`

**Interfaces:**
- Consumes: extraction/retrieval APIs.
- Produces: deterministic precision/recall/evidence-validity and recall@5 reports with model/runtime metadata.

- [ ] **Step 1: Create synthetic bilingual quality fixtures**

Include at least 30 Polish/English messages covering direct idea, explicit requirement, constraint, rejection, question, agent-only proposal, negation, supersession, irrelevant chatter, and secret canaries. Expected labels contain exact spans and roles; no private historical content enters Git.

- [ ] **Step 2: Write failing metric/report tests**

Assert exact calculations for candidate precision, explicit-intent recall, evidence validity, retrieval recall@5, mean latency, and peak model estimate metadata. A report with 89% precision must fail the gate; 90/80/100 must pass.

- [ ] **Step 3: Implement benchmark command**

`pmem benchmark --fixture ... --model ...` writes a timestamped JSON report under the local project data directory and prints aggregate metrics only. Add `--retrieval-only` and `--dry-run`. Model prompts/outputs remain out of repository logs.

- [ ] **Step 4: Run live Hammer benchmark**

Ensure only the Hammer model is active for project-memory processing, context length is 4096 or less, and parallelism is 1. Run the synthetic fixture. Record exact model identifier, latency, precision, recall, evidence validity, and whether the gate passes in `docs/BENCHMARK.md`; do not copy private prompts or outputs.

- [ ] **Step 5: If Hammer fails, test one smaller multilingual instruction model**

Research one current approximately 3B multilingual instruction model whose measured working set fits below 6 GB. Record its source and exact identifier, load it alone with context 4096, parallel 1, and a short TTL, then run the identical fixture. Proceed to Qwen only if this smaller model also misses the quality gate. Do not install more than one candidate.

- [ ] **Step 6: Run controlled Qwen comparison without simultaneous residency**

Unload only a project-memory-managed Hammer identifier, load Qwen with context 4096, parallel 1, and short TTL, run the same fixture, then unload the project-memory-managed Qwen identifier. Never unload unrelated user models. Record actual resource estimate and results. If the strict 6 GB working-set budget is exceeded, mark Qwen ineligible as the default regardless of quality.

- [ ] **Step 7: Evaluate installed Nomic retrieval on bilingual fixture**

Run retrieval metrics. If it misses the agreed gate, research and install one small multilingual embedding model authorized by the user, record its source/model ID, and repeat the identical fixture. Keep only the winning configured model.

- [ ] **Step 8: Run benchmark unit tests/full suite and update durable docs**

Run full unittest discovery and `git diff --check`.

---

### Task 8: SEP pilot, agent-rule integration, operations documentation, and completion audit

**Files:**
- Create: `docs/OPERATIONS.md`
- Modify: `STATUS.md`
- Modify: `docs/WORKLOG.md`
- Modify only through command: `<SEP_ROOT>/AGENTS.md`
- Local-only data: `~/.project-memory/projects/<sep-id>/...`

**Interfaces:**
- Consumes: installed `pmem` CLI and all application services.
- Produces: completed SEP memory index, installed preflight rules, verification evidence, and operational handoff.

- [ ] **Step 1: Register the SEP pilot and inspect configuration**

Run:

```bash
pmem init --root "<SEP_ROOT>" --name sep
pmem status --root "<SEP_ROOT>"
```

Verify config/vault/memory/receipt paths are outside the repo and modes are `0600`/`0700` as designed.

- [ ] **Step 2: Run complete Codex+Hermes backfill**

Run `pmem sync --root "<SEP_ROOT>" --extract`. Record counts by source/role, duration, pending/failed extraction counts, and model used. Do not record message bodies.

- [ ] **Step 3: Verify idempotency and incremental performance**

Run the identical sync again. Assert zero duplicate rows, unchanged source hashes, and no-change completion under 30 seconds. If the target fails, profile and fix the adapter/checkpoint bottleneck before continuing.

- [ ] **Step 4: Build private SEP acceptance fixture and run five retrieval checks**

Store expected source IDs and sanitized labels only under the restrictive local data directory. Questions cover lightweight-model rationale, dangerous-operation authority, escalation after unknown failure, full-output retention, and context-limit/state-recovery separation. Run `pmem search` and require expected evidence in the top five for each.

- [ ] **Step 5: Install and verify agent rules**

Run `pmem install-rules --root "<SEP_ROOT>" --create-agents` twice. Verify the second run produces no diff, existing rules are preserved, and the block points at the correct project root. Do not commit the SEP change.

- [ ] **Step 6: Exercise real preflight/conflict/correction workflow**

Run preflight for one historical SEP question, inspect cited objects, create one local correction/suppression test item if needed, and verify the receipt contains exact returned IDs/source hashes without secrets.

- [ ] **Step 7: Verify quote-only fallback end to end**

Temporarily configure the pilot with extraction disabled or a deliberately failing quality gate, then run `pmem search` and `pmem preflight`. Verify both return redacted source quotes without generated kinds/statements, missing-key errors, or extraction-related crashes. Restore the winning extraction configuration afterward.

- [ ] **Step 8: Write operations guide**

Document installation, project registration, start/end agent workflow, commands, privacy boundary, raw inspection confirmation, model configuration, rebuild/recovery, adapter limitations, and how to remove local data safely. Include exact verified commands only.

- [ ] **Step 9: Run full verification and independent code review**

Run:

```bash
python3 -m unittest discover -s tests -v
pmem status --root "<SEP_ROOT>"
git diff --check
git status --short
```

Write a targeted review brief listing exact changed files, tests, runtime evidence, risks, and excluded external changes. Run the configured Hermes code-reviewer. Fix valid findings and repeat until no blocking findings remain.

- [ ] **Step 10: Completion audit**

Map every design requirement and quality gate to current evidence in `STATUS.md`. Explicitly distinguish implemented, live-verified, deferred, and unverified behavior. Confirm no secret content, databases, raw fixtures, model output, or local receipts are present in Git status. Do not commit or push.
