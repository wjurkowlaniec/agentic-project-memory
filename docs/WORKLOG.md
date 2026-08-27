# Worklog

## 2026-08-27 — Discovery and design

- Captured project-memory MVP behavior and constraints; memory is an independent pre-agent layer and SEP remains deterministic execution/verification.
- Inspected SEP, Codex/Hermes source formats, local model availability, and project scoping requirements without copying private conversation bodies into this repository.
- Wrote the approved Project Memory MVP design. No live data backfill, model download, commit, or push.

## 2026-08-27 — Tasks 1–3 implementation

- Added package metadata, immutable records, project configuration/aliases, restrictive atomic persistence, versioned vault/search SQLite repositories, and deterministic versioned redaction.
- Added synthetic Codex/Hermes adapters with checkpointing, partial-tail handling, idempotency, rolling exports, and direct vault streaming.
- Added loopback-only LM Studio transport, strict extraction parsing/validation, exact evidence spans, authority checks, bounded prompts, indexed embeddings, and pending/failed extraction jobs.
- TDD focused suites and full suites passed after each implementation stage; compileall and diff checks passed. No private history was imported.

## 2026-08-27 — Tasks 4–5 implementation and reviews

- Added normalized float32 embedding persistence/search, derived knowledge storage/FTS, hybrid RRF retrieval, auditable citations, and project isolation.
- Added service orchestration, redacted model-facing sync, retryable jobs, preflight receipts, conflict/supersession handling, corrections, suppression, and derived rebuild behavior.
- Review rounds covered object-key identity, migration, exact evidence/source-session joins, query/suppression redaction, receipt rollback cleanup, extraction attempts, deterministic conflicts, canary markers, and rebuild limitations.
- Focused/full tests, compileall, and diff checks passed. No live data, staging, commit, or push.

## 2026-08-27 — Task 6 implementation and fixes

- Added CLI commands, compact cited output, raw-inspect confirmation, sanitized failures, pending status, exact rule markers, idempotent rule installation, multi-project config registry, and safe Hermes streaming.
- Fixed shell-safe rule roots, registry collisions, missing-model extraction guard, and atomic discard of partial Hermes exports on timeout/nonzero exit.
- Focused/full tests, compileall, diff checks, safe init smoke, and `pmem --help` passed. Task 7 and SEP remained untouched until this entry.

## 2026-08-27 — Task 7 benchmark framework

- Read the Task 7 brief, approved design, existing extraction/retrieval/config/CLI, and repository tests.
- Created 30 synthetic bilingual Polish/English messages covering direct ideas, explicit requirements, constraints, rejection, questions, agent proposals, negation, supersession, chatter, and secret canaries. No private historical content was added.
- Added deterministic metrics for candidate precision, explicit-intent recall, exact evidence validity, retrieval recall@5, latency, throughput, failed/pending rates, and runtime/model metadata. The gate is candidate precision >= 0.90, explicit-intent recall >= 0.80, exact evidence validity >= 1.00.
- Added actual `MemoryRepository` + `HybridRetriever` retrieval-fixture execution, with redaction before indexing and no report/log prompt, raw-output, or fixture-body persistence.
- Added project-config-aware `pmem benchmark` with mutually exclusive `--extraction-only`/`--retrieval-only`; combined model mode is rejected unless an undocumented explicit opt-in is supplied. Retrieval uses a fresh restrictive temporary DB under exports with synthetic project ID and removes DB/WAL/SHM before return; real `memory.sqlite3` is never opened. Gate failure returns 1 and execution/config/fixture failure returns 2. Reports are timestamped JSON under `ProjectPaths.exports_dir`, directory 0700 and file 0600.
- Added `docs/BENCHMARK.md` methodology and command placeholders; no fabricated live results.
- TDD RED: `.venv/bin/python -m unittest tests.test_benchmark -v` failed with `ModuleNotFoundError: No module named 'project_memory.benchmark'` before implementation.
- Focused GREEN: `.venv/bin/python -m unittest tests.test_benchmark tests.test_cli -v` — 23 tests passed.
- Full verification: `.venv/bin/python -m unittest discover -s tests -v` — 111 tests passed.
- `.venv/bin/python -m compileall -q src tests` — passed.
- Git checks intentionally not run for this safety-fix task.
- CLI dry-run smoke using temporary registered project/config — exit 0; aggregate metrics only; report mode 0600; no LM Studio contact.
- No live model run, model download/unload, SEP access, staging, commit, or push.

## 2026-08-27 — Live compatibility safety fix

- Changed `LMStudioClient.chat_json` from `json_object` to LM Studio-compatible strict `json_schema` output named `knowledge_extraction`; the schema covers the complete `KnowledgeCandidate` shape and forbids extra keys.
- Tightened completed extraction parsing to require every schema field and matching scalar/array types. The local synthetic HTTP server rejects `json_object`, accepts `json_schema`, and asserts the exact request schema.
- Changed benchmark semantics so any pending extraction is sanitized `model_unavailable` execution failure (CLI exit 2), with no partial report; invalid completed model output remains a quality-gate failure (exit 1).
- Added regression tests and updated benchmark documentation/status/report notes. No live calls, model changes/downloads, SEP access, staging, commit, or push.
- Verification: focused LM Studio/extraction/benchmark/CLI suite passed with 41 tests; full `.venv` suite passed with 114 tests; compileall and diff-hygiene checks passed.

## 2026-08-27 — Task 7 observed benchmark evidence and completion

- Hammer managed benchmark `pmem-hammer-bench`: 1.54 GiB loaded, 31 extraction calls, approximately 65.0 s total/extraction latency, `failed_rate=25/31=0.806452`, `candidate_precision=1.0` from zero predictions, `explicit_intent_recall=0.0`, and `exact_evidence_validity=1.0`; the quality gate failed. Aggregate report: `~/.project-memory/projects/<LOCAL_ID>/exports/benchmark-<local-report>.json`.
- Qwen 3.6 trusted-remote benchmark: `/models` was healthy in 0.024 s and the model was present, but the full run ended sanitized `operation_failed` without a report. Both the strict synthetic request and the user's simple 256-token request timed out after 90 s. Marked operationally unavailable with no quality verdict; no project or SEP history was sent.
- Corrected Nomic retrieval-only benchmark `pmem-nomic-bench`: 7 embedding calls, 6 queries, `recall@5=1.0`, total 418.127375 ms, gate pass, exit 0. Aggregate report: `~/.project-memory/projects/<LOCAL_ID>/exports/benchmark-<local-report>.json`.
- A pre-fix report exposed retrieval-only gate contamination and led to its correction; it is not valid evidence. Nomic was unloaded and the original user Hammer restored as `hammer2.1-1.5b-8-bit`, context 32768, parallel 4. Remote opt-in security boundary review approved. No private content or endpoint details were written.
- Full suite: 123 tests passed. Reviews approved. Task 7 complete; Task 8 next.

## 2026-08-27 — Task 8 live pilot and documentation

- Ran the source-only+Nomic pilot for project `<LOCAL_ID>`; 809 messages and 58 sessions completed in 56.71s. Source/role counts were Codex assistant545/user147 and Hermes assistant55/user62.
- Repeated sync rolled 117 messages across 55 Hermes sessions but kept the physical database at 809; duplicate groups remained 0, the source-hash digest was identical, and runtime was 6.20s. Pending extraction and embedding were both 0.
- Nomic `text-embedding-nomic-embed-text-v1.5` (80.21 MiB) passed retrieval recall@5 1.0 at 418.127 ms. The private 0600 acceptance fixture produced ranks 1,2,2,1,2 for all 5/5 expected results.
- Real preflight+inspect succeeded with a 0600 receipt, citations/source hashes populated, and `material_conflict=false`. Quote-only and pure FTS fallbacks succeeded; the redaction audit found 0 newly detectable secret matches.
- Extraction remained disabled because Hammer failed quality and Qwen 3.6 generation was operationally unavailable. No knowledge items exist; live correction/suppression remains deferred until a quality-approved extractor produces a source-backed item.
- Installed SEP rules twice into the previously absent `AGENTS.md`; the second run made 0 changes with identical hash. Verified global editable `pmem` installation via uv/Python 3.11 and status/help. Created `docs/OPERATIONS.md`, the Task 8 report, and the coding+Codex review brief. No commit or push.
