# Project Memory MVP Status

Last updated: 2026-08-27

## Objective

Maintain a local, project-scoped memory layer with a separate raw vault and redacted search index, and package it for safe public installation.

## Current state

- Core local storage, redaction, Codex/Hermes adapters, search, preflight, rules, and benchmark mechanics are implemented and covered by tests.
- Public packaging now includes an idempotent installer, README, installation prompt, and packaging review brief.
- Verified adapter scope is Codex + Hermes only.
- Extraction remains optional and disabled by default; source-only sync, FTS search, quote fallback, and preflight remain available.
- Benchmark documentation retains aggregate observations only; prompts, model output, private history, credentials, local identifiers, and report filenames are excluded.
- The installer does not read conversations, load models, synchronize data, extract content, or modify target code.

## Active phase

Public packaging verification: focused installer RED/GREEN tests, shell syntax, full unittest, compileall, temporary installer smoke test, diff hygiene, and privacy scan. No commit, stage, or push is part of this phase.

## Public verification boundary

Public examples use `<PROJECT_ROOT>`; `<SEP_ROOT>` is reserved for internal planning. Local project data, raw vaults, derived databases, receipts, model endpoints, and runtime credentials stay outside this repository.
