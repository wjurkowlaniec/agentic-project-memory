# Operations

Agentic Project Memory is a local, project-scoped memory layer. Project data is stored outside the repository with restrictive permissions. The raw vault is separate from a redacted SQLite/FTS index used for retrieval and model-facing operations.

## Install and register

```bash
./scripts/install.sh --project-root "<PROJECT_ROOT>"
```

The installer installs the checkout as an editable uv tool in normal mode, preserves an existing registration, and updates only the managed rules block. It never reads conversations, syncs, extracts, searches, preflights, loads models, or changes target code.

## Manual flow

Load Nomic in the local model runtime before embedding work. Then run:

```bash
pmem sync --root "<PROJECT_ROOT>"
pmem search --root "<PROJECT_ROOT>" <query words>
pmem preflight --root "<PROJECT_ROOT>" "<current user request>"
pmem status --root "<PROJECT_ROOT>"
```

Use `--extract` only after an extraction model passes the benchmark quality gate and is explicitly configured. Extraction is optional; source-only sync and FTS/quote fallback do not require it.

## Privacy

Do not place raw history, message bodies, model output, API keys, endpoint details, receipt IDs, object IDs, source digests, or local benchmark filenames in repository documentation. Raw inspection requires an explicit opt-in and should never be copied to logs or public artifacts.

## Scope and limits

Adapters currently support Codex and Hermes only. Commands are explicit and local; there is no daemon or cloud service. Benchmarks use synthetic fixtures and aggregate metrics, so results vary with model, hardware, residency, context, and endpoint availability.
