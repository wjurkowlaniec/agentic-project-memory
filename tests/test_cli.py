import contextlib
import io
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from project_memory import cli


class FakeService:
    def __init__(self):
        self.calls = []
        self.extractor = None

    def sync(self, **kwargs): self.calls.append(("sync", kwargs)); return {"messages": 2, "sessions": 1, "pending": 1, "warnings": []}
    def search(self, query, limit=10):
        self.calls.append(("search", query, limit))
        return [type("R", (), {"object_id": "message:x", "source": "codex", "session_id": "s", "timestamp": "now", "quote": "redacted quote", "state": "provisional", "kind": "observation", "inspect_command": "pmem inspect --root <project> message:x"})()]
    def preflight(self, query): self.calls.append(("preflight", query)); return {"citations": [{"source": "codex", "session_id": "s", "object_id": "message:x", "quote": "evidence", "timestamp": "now"}], "material_conflict": False}
    def inspect(self, object_id, **kwargs): self.calls.append(("inspect", object_id, kwargs)); return {"object_id": object_id, "content": "safe"}
    def suppress(self, item_id, reason): self.calls.append(("suppress", item_id, reason))
    def correct(self, item_id, statement, **kwargs): self.calls.append(("correct", item_id, statement, kwargs)); return {"item_id": "new", "state": "confirmed"}
    def status(self): return {"project_id": "p", "messages": 2, "pending_extraction": 1, "pending_embedding": 0}
    def rebuild(self): return "/tmp/backup"
    def close(self): pass


class CliTests(unittest.TestCase):
    def run_cli(self, argv, service=None):
        out = io.StringIO()
        with patch.object(cli, "service_factory", return_value=service or FakeService()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_help_lists_all_commands(self):
        code, output = self.run_cli(["--help"])
        self.assertEqual(code, 0)
        for command in ("init", "sync", "search", "preflight", "inspect", "suppress", "correct", "benchmark", "install-rules", "status", "rebuild", "commands"):
            self.assertIn(command, output)

    def test_commands_parser_supports_import_and_sorted_listing(self):
        parser = cli._parser()
        self.assertEqual(parser.parse_args(["commands", "import-history"]).command_action, "import-history")
        self.assertEqual(parser.parse_args(["commands"]).command_action, "list")

    def test_quote_compactor_preserves_short_exact_text_after_whitespace_normalization(self):
        self.assertEqual(cli._compact_display_quote("short quote"), "short quote")
        self.assertEqual(cli._compact_display_quote("  short\nquote\ttext  "), "short quote text")

    def test_search_quote_is_hard_bounded_with_explicit_truncation_marker(self):
        service = FakeService()
        long_quote = "secret " * 200
        service.search = lambda query, limit=10: [type("R", (), {
            "object_id": "message:x", "source": "codex", "session_id": "s", "quote": long_quote,
        })()]
        with tempfile.TemporaryDirectory() as tmp:
            code, output = self.run_cli(["search", "--root", tmp, "what"], service)
        displayed_quote = output.split("] ", 1)[1].rstrip("\n")
        self.assertEqual(code, 0)
        self.assertLessEqual(len(displayed_quote), 600)
        self.assertIn("[truncated]", displayed_quote)

    def test_preflight_quote_is_hard_bounded_with_explicit_truncation_marker(self):
        service = FakeService()
        long_quote = "evidence\\n" * 200
        service.preflight = lambda query, **kwargs: {"citations": [{
            "source": "codex", "session_id": "s", "object_id": "message:x", "quote": long_quote,
        }], "material_conflict": False}
        with tempfile.TemporaryDirectory() as tmp:
            code, output = self.run_cli(["preflight", "--root", tmp, "request"], service)
        displayed_quote = output.split("] ", 1)[1].rstrip("\n")
        self.assertEqual(code, 0)
        self.assertLessEqual(len(displayed_quote), 600)
        self.assertIn("[truncated]", displayed_quote)

    def test_search_is_compact_cited_and_not_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = self.run_cli(["search", "--root", tmp, "what", "--limit", "2"])
        self.assertEqual(code, 0)
        self.assertIn("[codex:s:message:x]", output)
        self.assertNotIn("{", output)

    def test_raw_inspect_requires_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = self.run_cli(["inspect", "--root", tmp, "message:x", "--raw"])
        self.assertNotEqual(code, 0)
        self.assertIn("--yes", output)
        code, output = self.run_cli(["inspect", "--root", tmp, "message:x", "--raw", "--yes"])
        self.assertEqual(code, 0)

    def test_pending_status_and_conflict_warning_are_human_output(self):
        service = FakeService()
        service.preflight = lambda query, **kwargs: {"citations": [], "material_conflict": True}
        with tempfile.TemporaryDirectory() as tmp:
            code, output = self.run_cli(["preflight", "--root", tmp, "request"], service)
        self.assertEqual(code, 0)
        self.assertIn("CONFLICT", output)
        code, output = self.run_cli(["status", "--root", tmp], service)
        self.assertEqual(code, 0)
        self.assertIn("pending extraction: 1", output)

    def test_benchmark_requires_registered_project_and_fixture(self):
        code, output = self.run_cli(["benchmark", "--root", "/tmp/x", "--fixture", "fixture", "--model", "model"])
        self.assertNotEqual(code, 0)
        self.assertIn("operation_failed", output)

    def test_sync_extract_without_model_fails_before_service_sync(self):
        service = FakeService()
        code, output = self.run_cli(["sync", "--root", "/tmp/x", "--extract"], service)
        self.assertNotEqual(code, 0)
        self.assertIn("extraction_model_not_configured", output)
        self.assertEqual(service.calls, [])

    def test_plain_sync_without_extraction_model_still_succeeds(self):
        service = FakeService()
        code, output = self.run_cli(["sync", "--root", "/tmp/x"], service)
        self.assertEqual(code, 0)
        self.assertEqual(service.calls, [("sync", {"extract": False})])
        self.assertIn("Synced 2 message(s)", output)

    def test_sync_uses_current_directory_when_root_is_omitted(self):
        service = FakeService()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("project_memory.cli.Path.cwd", return_value=Path(tmp)):
                code, output = self.run_cli(["sync"], service)
        self.assertEqual(code, 0)
        self.assertEqual(service.calls, [("sync", {"extract": False})])
        self.assertIn("Synced 2 message(s)", output)

    def test_benchmark_modes_are_mutually_exclusive_at_cli_boundary(self):
        code, output = self.run_cli(["benchmark", "--root", "/tmp/x", "--fixture", "fixture", "--model", "model", "--extraction-only", "--retrieval-only"])
        self.assertEqual(code, 2)
        self.assertIn("not allowed with argument", output)

    def test_combined_benchmark_opt_in_is_not_in_help(self):
        parser = cli._parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["benchmark", "--help"])

    def test_pending_benchmark_is_exit_two_without_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = type("Config", (), {"root": root, "model_names": {}})()
            with patch.object(cli, "load_project_config", return_value=config), \
                 patch.object(cli, "load_fixture", return_value=object()), \
                 patch.object(cli, "LMStudioClient"), \
                 patch.object(cli, "BenchmarkRunner", side_effect=cli.BenchmarkError("model_unavailable")), \
                 patch.object(cli, "write_report") as write_report, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(["benchmark", "--root", str(root), "--fixture", "fixture", "--model", "model", "--extraction-only"])
        self.assertEqual(code, 2)
        self.assertIn("error: operation_failed", output.getvalue())
        write_report.assert_not_called()

    def test_remote_benchmark_requires_flag_and_environment_key_before_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = type("Config", (), {"root": root, "model_names": {}})()
            with patch.object(cli, "load_project_config", return_value=config), \
                 patch.object(cli, "load_fixture", return_value=object()), \
                 patch.object(cli, "LMStudioClient") as client, \
                 patch.dict(os.environ, {}, clear=True), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                client.is_loopback_url.return_value = False
                code = cli.main(["benchmark", "--root", str(root), "--fixture", "fixture", "--model", "model", "--base-url", "https://api.example.test/v1", "--extraction-only"])
        self.assertEqual(code, 2)
        self.assertIn("operation_failed", output.getvalue())
        client.assert_not_called()

    def test_remote_benchmark_reads_key_only_from_environment_and_passes_safe_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = type("Config", (), {"root": root, "model_names": {}})()
            result = {"metrics": {}, "quality_eligible": True, "gate_passed": True}
            with patch.object(cli, "load_project_config", return_value=config), \
                 patch.object(cli, "load_fixture", return_value=object()), \
                 patch.object(cli, "LMStudioClient") as client, \
                 patch.object(cli, "BenchmarkRunner") as runner, \
                 patch.object(cli, "write_report", return_value=Path("report.json")), \
                 patch.dict(os.environ, {"PMEM_API_KEY": "test-secret"}, clear=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                runner.return_value.run.return_value = result
                code = cli.main(["benchmark", "--root", str(root), "--fixture", "fixture", "--model", "model", "--base-url", "https://api.example.test/v1", "--allow-remote", "--extraction-only"])
        self.assertEqual(code, 0)
        client.assert_called_once_with("https://api.example.test/v1", allow_remote=True, api_key="test-secret")
        self.assertNotIn("test-secret", repr(result))


if __name__ == "__main__":
    unittest.main()
