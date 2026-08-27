# Agentic Project Memory Public Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the local Project Memory MVP as a privacy-scrubbed public GitHub repository with a safe, idempotent installer and verified documentation.

**Architecture:** Keep the existing `pmem` CLI and local raw-vault/redacted-index boundary unchanged. Add a strict Bash installer that discovers the installed `pmem` executable from `uv tool dir --bin`, registers the target only when absent, and installs rules without synchronization or model work. Test installer behavior through a fake `pmem` executable and controlled skip-install mode.

**Tech Stack:** Bash, Python 3.11+, uv, setuptools, unittest, existing `pmem` CLI.

---

### Task 1: Installer contract tests (RED)

**Files:**
- Create: `tests/test_installer.py`

- [ ] **Step 1: Write subprocess tests for required arguments and syntax.**
- [ ] **Step 2: Write temp-target tests for init, status, rules, optional embedding env, and exact command ordering.**
- [ ] **Step 3: Write tests proving existing registration is preserved, `--no-rules` skips rules, and no sync/extract/model calls occur.**
- [ ] **Step 4: Run focused tests and confirm they fail because `scripts/install.sh` is absent.**

### Task 2: Minimal installer implementation (GREEN)

**Files:**
- Create: `scripts/install.sh`
- Modify: `src/project_memory/cli.py` only if installer needs an existing public command contract

- [ ] **Step 1: Implement Bash strict mode, shell-safe arrays, argument validation, and path-with-spaces handling.**
- [ ] **Step 2: Implement normal uv prerequisite/tool installation and executable discovery via `uv tool dir --bin`.**
- [ ] **Step 3: Implement registration detection without reading conversations; initialize only when absent and preserve existing config.**
- [ ] **Step 4: Install idempotent AGENTS rules unless disabled and print manual Nomic load/sync/search/preflight commands.**
- [ ] **Step 5: Run focused installer tests and make them pass.**

### Task 3: Public documentation and metadata

**Files:**
- Create: `README.md`
- Create: `docs/INSTALL_PROMPT.md`
- Create: `.superpowers/review-brief-public-packaging.md`
- Modify: `pyproject.toml`
- Modify: `STATUS.md`, `docs/OPERATIONS.md`, `docs/BENCHMARK.md`, `docs/WORKLOG.md`, and candidate design/report docs as needed

- [ ] **Step 1: Add concise README architecture, scope, privacy boundary, fallback, quick start, manual commands, caveats, and GitHub URL.**
- [ ] **Step 2: Add the Polish self-install prompt with explicit prohibitions and reporting requirements.**
- [ ] **Step 3: Update package readme metadata and correct stale active-phase claims.**
- [ ] **Step 4: Scrub public candidate docs of local absolute paths, exact report filenames, project IDs, private identifiers, endpoint details, and secrets while retaining aggregate metrics and synthetic canaries.**
- [ ] **Step 5: Write the packaging review brief and inspect the diff.**

### Task 4: Verification

**Files:**
- No additional files

- [ ] **Step 1: Run focused RED/GREEN installer tests.**
- [ ] **Step 2: Run `bash -n scripts/install.sh`.**
- [ ] **Step 3: Run full unittest discovery and `compileall`.**
- [ ] **Step 4: Run installer temp smoke tests with fake `pmem` and `--skip-tool-install`.**
- [ ] **Step 5: Run `git diff --check` and a public privacy scan.**
- [ ] **Step 6: Confirm no commit, staging, push, target-project access, conversation sync, extraction, or model loading occurred.**
