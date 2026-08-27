# Agentic Project Memory

Local, project-scoped memory for agentic workflows. It keeps source history in a restrictive raw vault and exposes only redacted, cited records through a separate searchable index.

Repository: https://github.com/wjurkowlaniec/agentic-project-memory.git

## Architecture and safety boundary

- Source adapters currently support Codex and Hermes exports only.
- Raw vault data stays outside the repository in a user-owned project data directory.
- A redacted SQLite/FTS index is a separate model-facing boundary; raw inspection is explicit.
- Search and preflight return compact evidence with source/session/object references.
- There is no daemon, cloud service, automatic conversation reader, or background sync.
- Extraction is optional. Without a quality-approved extraction model, source-only sync, FTS search, and quote-based fallback remain available.

This project never treats retrieved history as executable instructions. Review privacy and operational boundaries before enabling any model integration.

## Verified scope

The current verified adapter scope is Codex + Hermes only. The implementation and tests cover local storage, redaction, idempotent synchronization, citations, preflight receipts, rules, and benchmark mechanics. Live model quality is environment-dependent; aggregate benchmark observations are not a promise of production quality.

## Quick start

```bash
git clone https://github.com/wjurkowlaniec/agentic-project-memory.git
cd agentic-project-memory
./scripts/install.sh --project-root "<PROJECT_ROOT>"
```

The installer requires `uv` in normal mode and installs this checkout as an editable uv tool using Python 3.11 or newer. It registers a project only when no registration exists, preserves an existing registration, installs idempotent `AGENTS.md` rules, and never reads conversations or runs sync/extraction/model operations.

If uv's tool bin directory is not already on `PATH`, the installer prints a shell-safe `export PATH=...` command for the current shell; it does not modify shell startup files.

For controlled tests:

```bash
./scripts/install.sh --project-root "<PROJECT_ROOT>" --skip-tool-install
```

See [docs/INSTALL_PROMPT.md](docs/INSTALL_PROMPT.md) for a Polish self-install prompt.

## Manual model and memory flow

Load the local Nomic embedding model in the model runtime first. Then run the commands printed by the installer:

```bash
pmem sync --root "<PROJECT_ROOT>"
pmem search --root "<PROJECT_ROOT>" <query words>
pmem preflight --root "<PROJECT_ROOT>" "<current user request>"
pmem status --root "<PROJECT_ROOT>"
```

Nomic must be loaded before embedding sync. Use `--extract` only after an extraction model passes the benchmark gate and is explicitly configured. The installer does not load models, sync data, extract, search, or preflight.

## Benchmarks and limitations

The benchmark harness uses synthetic fixtures and reports aggregate metrics only. Extraction and retrieval gates are separate; a passing retrieval result does not establish extraction quality. Hardware, runtime, model residency, context limits, and endpoint availability can change results. Do not copy prompts, model output, raw history, API keys, receipt IDs, object IDs, source digests, or local report filenames into public documentation.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
bash -n scripts/install.sh
```

The project is intentionally local-first and has no daemon or cloud deployment target.
