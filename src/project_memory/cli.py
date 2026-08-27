from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Sequence

from .adapters.codex import CodexAdapter
from .adapters.hermes import HermesAdapter
from .adapters.devin import DevinAdapter
from .adapters.windsurf import WindsurfAdapter, capture_windsurf_event
from .adapters.claude import ClaudeAdapter
from .benchmark import BenchmarkError, BenchmarkRunner, load_fixture, write_report
from .config import ProjectConfig, ProjectPaths, load_project_config, migrate_legacy_project, save_project_config
from .extraction import ExtractionEngine
from .lmstudio import LMStudioClient
from .rules import install_rules
from .service import ProjectMemoryService
from .discovery import claude_project_dir, devin_database, discover_sources

COMMANDS = ("init", "sync", "search", "preflight", "inspect", "suppress", "correct", "benchmark", "install-rules", "status", "rebuild", "capture-windsurf", "discover", "migrate-local")
_DISPLAY_QUOTE_LIMIT = 600
_DISPLAY_TRUNCATION_MARKER = " …[truncated]"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pmem", description="Local, project-scoped memory")
    subs = parser.add_subparsers(dest="command")
    init = subs.add_parser("init"); init.add_argument("--root"); init.add_argument("--name", required=True); init.add_argument("--alias", action="append", default=[])
    for name in ("sync", "status", "rebuild"):
        p = subs.add_parser(name); p.add_argument("--root")
        if name == "sync": p.add_argument("--extract", action="store_true")
    search = subs.add_parser("search"); search.add_argument("--root"); search.add_argument("query", nargs="+"); search.add_argument("--limit", type=int, default=10)
    pre = subs.add_parser("preflight"); pre.add_argument("--root"); pre.add_argument("query", nargs="+"); pre.add_argument("--limit", type=int, default=10)
    inspect = subs.add_parser("inspect"); inspect.add_argument("--root"); inspect.add_argument("object_id"); inspect.add_argument("--raw", action="store_true"); inspect.add_argument("--yes", action="store_true")
    suppress = subs.add_parser("suppress"); suppress.add_argument("--root"); suppress.add_argument("item_id"); suppress.add_argument("--reason", required=True)
    correct = subs.add_parser("correct"); correct.add_argument("--root"); correct.add_argument("item_id"); correct.add_argument("--statement", required=True)
    bench = subs.add_parser("benchmark"); bench.add_argument("--root"); bench.add_argument("--fixture", required=True); bench.add_argument("--model", required=True); bench.add_argument("--embedding-model"); bench.add_argument("--base-url", default="http://127.0.0.1:1234/v1"); bench.add_argument("--allow-remote", action="store_true"); bench.add_argument("--dry-run", action="store_true")
    modes = bench.add_mutually_exclusive_group(); modes.add_argument("--extraction-only", action="store_true"); modes.add_argument("--retrieval-only", action="store_true")
    bench.add_argument("--allow-combined-models", action="store_true", help=argparse.SUPPRESS)
    rules = subs.add_parser("install-rules"); rules.add_argument("--root"); rules.add_argument("--create-agents", action="store_true")
    capture = subs.add_parser("capture-windsurf"); capture.add_argument("--root")
    discover = subs.add_parser("discover"); discover.add_argument("--root")
    migrate = subs.add_parser("migrate-local"); migrate.add_argument("--root")
    return parser


def service_factory(root: Path) -> ProjectMemoryService:
    config = load_project_config(root)
    paths = ProjectPaths.for_root(config.root)
    adapters = {
        "codex": CodexAdapter(),
        "hermes": HermesAdapter(),
        "devin": DevinAdapter(),
        "windsurf": WindsurfAdapter(),
        "claude": ClaudeAdapter(),
    }
    client = LMStudioClient()
    extraction_model = config.model_names.get("extraction")
    embedding_model = config.model_names.get("embedding")
    extractor = ExtractionEngine(client, extraction_model) if extraction_model else None
    embedder = client if embedding_model else None
    return ProjectMemoryService.init(config, paths, adapters=adapters, extractor=extractor,
                                     embedder=embedder, extraction_enabled=bool(extractor),
                                     embedding_model=embedding_model or "nomic")


def _project_root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else Path.cwd().resolve()


def _compact_display_quote(value: object) -> str:
    """Normalize and bound a quote for human-facing CLI output only."""
    quote = re.sub(r"\s+", " ", str(value)).strip()
    if len(quote) <= _DISPLAY_QUOTE_LIMIT:
        return quote
    prefix_limit = _DISPLAY_QUOTE_LIMIT - len(_DISPLAY_TRUNCATION_MARKER)
    return quote[:prefix_limit].rstrip() + _DISPLAY_TRUNCATION_MARKER


def _print_citation(result: object) -> None:
    source = getattr(result, "source", "unknown")
    session = getattr(result, "session_id", "unknown")
    object_id = getattr(result, "object_id", "unknown")
    quote = _compact_display_quote(getattr(result, "quote", ""))
    print(f"[{source}:{session}:{object_id}] {quote}")


def _print_preflight(payload: dict[str, object]) -> None:
    if payload.get("material_conflict"):
        print("CONFLICT: confirmed historical intent requires user confirmation.")
    for citation in payload.get("citations", []):
        if isinstance(citation, dict):
            quote = _compact_display_quote(citation.get("quote", ""))
            print(f"[{citation.get('source', 'unknown')}:{citation.get('session_id', 'unknown')}:{citation.get('object_id', 'unknown')}] {quote}")
    if not payload.get("citations"):
        print("No matching project evidence.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code is None else int(exc.code)
    if not args.command:
        parser.print_help(); return 0
    try:
        if args.command == "init":
            root = _project_root(args.root)
            paths = ProjectPaths.for_root(root)
            config = ProjectConfig(
                name=args.name, root=root,
                aliases=tuple(Path(alias).expanduser().resolve() for alias in args.alias),
                source_locations={
                    "codex": str(Path.home() / ".codex" / "sessions"),
                    "devin": str(devin_database()),
                    "windsurf": str(paths.project_dir / "sources" / "windsurf"),
                    "claude": str(claude_project_dir(root)),
                },
                model_names={key: value for key, value in {
                    "extraction": os.environ.get("PMEM_EXTRACTION_MODEL", ""),
                    "embedding": os.environ.get("PMEM_EMBEDDING_MODEL", ""),
                }.items() if value},
            )
            save_project_config(config, paths)
            print(f"Initialized project {config.name} ({config.project_id}) at {config.root}")
            return 0
        if args.command == "migrate-local":
            root = _project_root(args.root)
            print("Migrated legacy project memory locally." if migrate_legacy_project(root) else "No legacy project memory to migrate.")
            return 0
        if args.command == "discover":
            root = _project_root(args.root)
            try:
                config = load_project_config(root)
                windsurf_inbox = Path(config.source_locations.get("windsurf", "")).expanduser() if config.source_locations.get("windsurf") else None
                claude_dirs = [Path(config.source_locations["claude"]).expanduser()] if config.source_locations.get("claude") else None
            except ValueError:
                windsurf_inbox = None
                claude_dirs = None
            for item in discover_sources(root, claude_dirs=claude_dirs, windsurf_inbox=windsurf_inbox):
                state = "available" if item["available"] else "not found"
                suffix = "; hook configured" if item.get("hook_configured") else ""
                print(f"{item['source']}: {state}{suffix}")
            return 0
        if args.command == "capture-windsurf":
            config = load_project_config(_project_root(args.root))
            paths = ProjectPaths.for_root(config.root)
            inbox = Path(config.source_locations.get("windsurf", str(paths.project_dir / "sources" / "windsurf"))).expanduser()
            event = __import__("json").load(sys.stdin)
            captured = capture_windsurf_event(event, transcript_root=os.environ.get("PMEM_WINDSURF_TRANSCRIPTS_HOME"), inbox_dir=inbox)
            print(f"Captured Windsurf transcript: {captured}")
            return 0
        if args.command == "install-rules":
            paths = install_rules(_project_root(args.root), create_agents=args.create_agents)
            print(f"Installed project-memory rules in {len(paths)} file(s).")
            return 0
        if args.command == "benchmark":
            config = load_project_config(_project_root(args.root))
            paths = ProjectPaths.for_root(config.root)
            fixture = load_fixture(Path(args.fixture))
            embedding_model = None if args.extraction_only else (args.embedding_model or config.model_names.get("embedding"))
            if args.dry_run:
                client = None
            else:
                api_key = os.environ.get("PMEM_API_KEY")
                if not LMStudioClient.is_loopback_url(args.base_url) and (not args.allow_remote or not api_key):
                    raise BenchmarkError("remote_endpoint_requires_explicit_opt_in")
                client = LMStudioClient(args.base_url, allow_remote=args.allow_remote, api_key=api_key)
            result = BenchmarkRunner(config.root, fixture, args.model, dry_run=args.dry_run, extraction_only=args.extraction_only, retrieval_only=args.retrieval_only, allow_combined_models=args.allow_combined_models, paths=paths, client=client, embedding_model=embedding_model).run()
            report = write_report(paths.exports_dir, result)
            metrics = result["metrics"]
            print("benchmark: " + " ".join(f"{key}={metrics[key]:.6f}" for key in sorted(metrics) if isinstance(metrics[key], (int, float))))
            print(f"report={report}")
            if not result["quality_eligible"]:
                print("INELIGIBLE: mechanics-only run; no model-quality gate was evaluated.")
            return 0 if result["gate_passed"] is not False else 1
        service = service_factory(_project_root(args.root))
        try:
            if args.command == "sync":
                if args.extract and service.extractor is None:
                    raise ValueError("extraction_model_not_configured")
                result = service.sync(extract=args.extract if hasattr(args, "extract") else None)
                print(f"Synced {result.get('messages', 0)} message(s) in {result.get('sessions', 0)} session(s); pending extraction: {result.get('pending', 0)}.")
                return 0
            if args.command == "search":
                for result in service.search(" ".join(args.query), args.limit): _print_citation(result)
                return 0
            if args.command == "preflight":
                _print_preflight(service.preflight(" ".join(args.query), limit=args.limit)); return 0
            if args.command == "inspect":
                if args.raw and not args.yes:
                    print("Raw inspection requires explicit --yes."); return 2
                row = service.inspect(args.object_id, raw=args.raw)
                if row is None: print("Object not found."); return 1
                if args.raw: print(str(row.get("content", "")))
                else: print(f"{args.object_id}: {row.get('state', row.get('role', 'available'))}")
                return 0
            if args.command == "suppress":
                service.suppress(args.item_id, args.reason); print(f"Suppressed {args.item_id}."); return 0
            if args.command == "correct":
                result = service.correct(args.item_id, args.statement, confirmed=True); print(f"Confirmed correction {result['item_id']}."); return 0
            if args.command == "status":
                status = service.status(); print(f"Project {status['project_id']}: messages: {status['messages']}; pending extraction: {status['pending_extraction']}; pending embedding: {status['pending_embedding']}."); return 0
            if args.command == "rebuild":
                service.rebuild(); print("Rebuilt derived memory database."); return 0
        finally:
            service.close()
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, BenchmarkError) as exc:
        code = str(exc) if str(exc) == "extraction_model_not_configured" else "operation_failed"
        print(f"error: {code}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
