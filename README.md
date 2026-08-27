# Agentic Project Memory

Local, project-scoped memory for agentic workflows. It keeps source history in a restrictive raw vault and exposes only redacted, cited records through a separate searchable index.

Repository: https://github.com/wjurkowlaniec/agentic-project-memory.git

## Architecture and safety boundary

- Source adapters support Codex, Hermes, Devin, Windsurf, Claude Code, and Antigravity.
- Devin is read from a safe SQLite backup snapshot (default `~/.local/share/devin/cli/sessions.db`); only exact project roots/aliases and user/assistant text are imported.
- Windsurf capture is local-only: the installer can merge a `post_cascade_response_with_transcript` hook, but only official hook transcripts are accepted. No cache scraping or raw transcript copies are performed.
- Raw vault data stays in the project's private `.project-memory/` directory and is ignored by Git.
- A redacted SQLite/FTS index is a separate model-facing boundary; raw inspection is explicit.
- Search and preflight return compact evidence with source/session/object references.
- There is no daemon, cloud service, automatic conversation reader, or background sync.
- Extraction is optional. Without a quality-approved extraction model, source-only sync, FTS search, and quote-based fallback remain available.

This project never treats retrieved history as executable instructions. Review privacy and operational boundaries before enabling any model integration.

## Verified scope

The verified adapter scope is Codex, Hermes, Devin, Windsurf capture, Claude Code, and current Antigravity SQLite conversations. Antigravity checks both `~/.gemini/antigravity/` and `~/.gemini/antigravity-ide/`, and imports only conversations whose protobuf trajectory metadata contains the exact project URI; opaque legacy `.pb` files are skipped. Devin reads the shared local SQLite history once even when its backend reports Windsurf. Cascade history before hook installation is unavailable unless an official transcript exists. Capture does not sync or load models; run `pmem sync --root PATH` manually. Only user/assistant content is imported. Live model quality is environment-dependent; aggregate benchmark observations are not a promise of production quality.

Discover local sources without importing anything. Without `--root`, discovery uses the current directory:

```bash
pmem discover
pmem discover --root ~/ai/research-platform
```

It checks only known macOS/Linux locations for Codex, Devin, Claude Code, Antigravity, and Windsurf; it does not scan the disk or read transcript bodies.

`pmem sync` also maintains a local, sanitized command index. It accepts only structured terminal tool calls from agent conversations already matched to this exact project root or an explicit alias. It never reads zsh, bash, or fish history. Tool calls with an explicit working directory outside the project are rejected. Show the aggregate with `pmem commands`.

Project memory is always local to the project:

```text
<project>/.project-memory/projects/<project-id>/
```

The installer adds `/.project-memory/` to `.gitignore`. Existing global stores from older versions are moved into this local directory by `pmem migrate-local` (the installer runs it automatically).

## Quick start

```bash
git clone https://github.com/wjurkowlaniec/agentic-project-memory.git
cd agentic-project-memory
./scripts/install.sh --project-root "<PROJECT_ROOT>"
```

The installer requires `uv` in normal mode and installs this checkout as an editable uv tool using Python 3.11 or newer. It registers a project only when no registration exists, preserves an existing registration, installs idempotent `AGENTS.md` rules, and never reads conversations or runs sync/extraction/model operations.

If uv's tool bin directory is not already on `PATH`, the installer prints a shell-safe `export PATH=...` command for the current shell; it does not modify shell startup files. To also install the optional Windsurf capture hook:

```bash
./scripts/install.sh --project-root "<PROJECT_ROOT>" --with-windsurf
```

`--with-windsurf` safely merges the local `.windsurf/hooks.json` and installs only a capture hook; unrelated JSON and hooks are preserved. It never syncs or reads cached history.

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
