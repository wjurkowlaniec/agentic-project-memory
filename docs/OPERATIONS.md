# Operations

Agentic Project Memory is a local, project-scoped memory layer. Project data is stored in `<PROJECT_ROOT>/.project-memory/` with restrictive permissions and the installer adds that directory to `.gitignore`. The raw vault is separate from a redacted SQLite/FTS index used for retrieval and model-facing operations.

## Install and register

```bash
./scripts/install.sh --project-root "<PROJECT_ROOT>"
```

The installer installs the checkout as an editable uv tool in normal mode, preserves an existing registration, and updates only the managed rules block. It never reads conversations, syncs, extracts, searches, preflights, loads models, or changes target code.

## Manual flow

Load Nomic in the local model runtime before embedding work. Then run:

```bash
pmem sync
pmem search <query words>
pmem preflight "<current user request>"
pmem status
```

Use `--extract` only after an extraction model passes the benchmark quality gate and is explicitly configured. Extraction is optional; source-only sync and FTS/quote fallback do not require it.

## Privacy

Do not place raw history, message bodies, model output, API keys, endpoint details, receipt IDs, object IDs, source digests, or local benchmark filenames in repository documentation. Raw inspection requires an explicit opt-in and should never be copied to logs or public artifacts.

## Scope and limits

Adapters support Codex, Hermes, Devin, Windsurf capture, and Claude Code. Commands are explicit and local; there is no daemon or cloud service. Benchmarks use synthetic fixtures and aggregate metrics, so results vary with model, hardware, residency, context, and endpoint availability.
