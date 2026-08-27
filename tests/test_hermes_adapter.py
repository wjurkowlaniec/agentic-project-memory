from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from project_memory.adapters.hermes import HermesAdapter
from project_memory.config import ProjectConfig



class HermesAdapterTests(unittest.TestCase):
    def test_initial_export_command_and_redacted_message_extraction(self):
        fixture = (Path(__file__).parent / "fixtures" / "hermes_export.jsonl").read_text()
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, fixture, "")

        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig.create("synthetic", Path("/synthetic/project"))
            result = HermesAdapter(command_runner=runner).scan(config, {})

        self.assertEqual(calls[0][0], ["hermes", "sessions", "export", "--format", "jsonl", "--cwd", str(config.root), "--yes", "-"])
        self.assertEqual([m.message_id for m in result.messages], ["h-user", "h-assistant"])
        self.assertTrue(all(m.project_id == config.project_id for m in result.messages))
        self.assertTrue(all(m.role in {"user", "assistant"} for m in result.messages))
        self.assertNotIn("TOOL BODY", " ".join(m.content for m in result.messages))
        self.assertEqual(result.sessions_seen, 1)

    def test_scan_preserves_exact_content_for_service_redaction(self):
        secret = "Authorization: Bearer sk-live-secret"
        record = {"session_id": "s", "cwd": "/synthetic/project", "updated_at": "now",
                  "messages": [{"id": "m", "role": "user", "content": secret}]}
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, json.dumps(record) + "\n", "")
        result = HermesAdapter(command_runner=runner).scan(
            ProjectConfig.create("synthetic", Path("/synthetic/project")), {})
        self.assertEqual(result.messages[0].content, secret)

    def test_incremental_window_replaces_updated_message_and_reports_failure_without_body(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 3, "SECRET EXPORT BODY", "synthetic exporter failed")

        config = ProjectConfig.create("synthetic", Path("/synthetic/project"))
        result = HermesAdapter(command_runner=runner).scan(config, {"newer_than": "2026-08-27T00:00:00Z", "command_index_version": 1})
        self.assertIn("--newer-than", calls[0])
        self.assertEqual(result.messages, ())
        self.assertTrue(any("exit 3" in warning for warning in result.warnings))
        self.assertNotIn("SECRET EXPORT BODY", " ".join(result.warnings))

    def test_updated_export_record_replaces_older_message_revision(self):
        first = {"session_id": "s", "cwd": "/synthetic/project", "updated_at": "2026-08-27T12:00:00Z",
                 "messages": [{"id": "m", "role": "user", "content": "old"}]}
        second = {"session_id": "s", "cwd": "/synthetic/project", "updated_at": "2026-08-27T13:00:00Z",
                  "messages": [{"id": "m", "role": "user", "content": "new"}]}
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, json.dumps(first) + "\n" + json.dumps(second), "")

        result = HermesAdapter(command_runner=runner).scan(
            ProjectConfig.create("synthetic", Path("/synthetic/project")), {})
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].content, "new")

    def test_message_timestamps_checkpoint_exports_without_session_updated_at(self):
        record = {"session_id": "s", "cwd": "/synthetic/project", "messages": [
            {"id": "u", "role": "user", "timestamp": "2026-08-27T12:00:00Z", "content": "first"},
            {"id": "a", "role": "assistant", "timestamp": "2026-08-27T12:01:00Z", "content": "second"},
        ]}
        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps(record), "")

        adapter = HermesAdapter(command_runner=runner)
        config = ProjectConfig.create("synthetic", Path("/synthetic/project"))
        first = adapter.scan(config, {})
        second = adapter.scan(config, first.next_state)

        self.assertEqual([item.content for item in first.messages], ["first", "second"])
        self.assertEqual(second.messages, ())
        self.assertEqual(second.sessions_seen, 0)
        self.assertEqual(first.next_state["newer_than"], "2026-08-27T12:01:00Z")
        self.assertIn("--newer-than", commands[1])

    def test_extracts_terminal_commands_only_from_exact_project_export(self):
        matching = {"session_id": "project-session", "cwd": "/synthetic/project", "messages": [
            {"id": "assistant", "role": "assistant", "timestamp": "2026-08-27T12:01:00Z", "content": "done", "tool_calls": [
                {"id": "tool-good", "function": {"name": "terminal", "arguments": {"command": "git status", "workdir": "/synthetic/project/subdir"}}},
                {"id": "tool-outside", "function": {"name": "terminal", "arguments": {"command": "cat secret", "workdir": "/other/project"}}},
            ]},
        ]}
        unrelated = {"session_id": "other-session", "cwd": "/other/project", "messages": [
            {"id": "assistant", "role": "assistant", "content": "done", "tool_calls": [
                {"id": "tool-wrong-session", "function": {"name": "terminal", "arguments": {"command": "npm test"}}},
            ]},
        ]}
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, json.dumps(matching) + "\n" + json.dumps(unrelated), "")

        result = HermesAdapter(command_runner=runner).scan(
            ProjectConfig.create("synthetic", Path("/synthetic/project")), {})

        self.assertEqual([(c.command_id, c.command) for c in result.commands], [("tool-good", "git status")])
        self.assertEqual(result.commands[0].timestamp, "2026-08-27T12:01:00Z")
        self.assertEqual(result.commands[0].cwd, "/synthetic/project/subdir")

    def test_command_backfill_ignores_old_newer_than_checkpoint_once(self):
        record = {"session_id": "project-session", "cwd": "/synthetic/project", "messages": [
            {"id": "assistant", "role": "assistant", "timestamp": "2026-08-20T12:00:00Z", "content": "done", "tool_calls": [
                {"id": "old-tool", "function": {"name": "terminal", "arguments": {"command": "git status"}}},
            ]},
        ]}
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps(record), "")

        adapter = HermesAdapter(command_runner=runner)
        config = ProjectConfig.create("synthetic", Path("/synthetic/project"))
        first = adapter.scan(config, {"newer_than": "2026-08-27T00:00:00Z"})
        second = adapter.scan(config, first.next_state)

        self.assertNotIn("--newer-than", calls[0])
        self.assertIn("--newer-than", calls[1])
        self.assertEqual([command.command for command in first.commands], ["git status"])
        self.assertEqual(second.commands, ())
        self.assertEqual(first.next_state["command_index_version"], 1)

    def test_unredacted_export_is_sent_directly_to_injected_vault_writer(self):
        received = []
        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "SYNTHETIC RAW BODY", "")

        adapter = HermesAdapter(command_runner=runner)
        ok = adapter.archive_unredacted(
            ProjectConfig.create("synthetic", Path("/synthetic/project")), received.append)
        self.assertTrue(ok)
        self.assertEqual(received, ["SYNTHETIC RAW BODY"])
        self.assertNotIn("--redact", commands[0])

    def test_unredacted_export_streams_chunks_through_injectable_runner(self):
        class StreamProcess:
            returncode = 0
            stderr = iter(["must not be read"])
            def __init__(self):
                self.stdout = iter(["first\n", "second\n"])
            def wait(self, timeout=None):
                return self.returncode

        calls = []
        def streaming_runner(command, **kwargs):
            calls.append((command, kwargs))
            return StreamProcess()

        received = []
        ok = HermesAdapter(streaming_runner=streaming_runner).archive_unredacted(
            ProjectConfig.create("synthetic", Path("/synthetic/project")), received.append)
        self.assertTrue(ok)
        self.assertEqual(received, ["first\n", "second\n"])
        self.assertEqual(calls[0][1]["stdout"], subprocess.PIPE)
        self.assertEqual(calls[0][1]["stderr"], subprocess.DEVNULL)

    def test_unredacted_export_timeout_kills_blocked_real_exporter_promptly(self):
        def streaming_runner(command, **kwargs):
            del command
            kwargs.pop("timeout", None)
            process = subprocess.Popen(
                [sys.executable, "-c", "import sys,time; print('first', flush=True); time.sleep(5)"],
                **kwargs,
            )

            class ProcessProxy:
                returncode = 0
                stdout = process.stdout

                def wait(self, timeout=None):
                    return process.wait(timeout=timeout)

                def kill(self):
                    return process.kill()

            return ProcessProxy()

        received = []
        started = time.monotonic()
        ok = HermesAdapter(streaming_runner=streaming_runner, timeout=0.15).archive_unredacted(
            ProjectConfig.create("synthetic", Path("/synthetic/project")), received.append)
        elapsed = time.monotonic() - started

        self.assertFalse(ok)
        self.assertEqual(received, ["first\n"])
        self.assertLess(elapsed, 1.5)

    def test_project_id_and_canonical_cwd_are_used_by_both_adapters(self):
        config = ProjectConfig.create("display-name", Path("/synthetic/project/../project"))
        self.assertNotEqual(config.project_id, config.name)

    def test_scan_streams_jsonl_from_real_synthetic_subprocess(self):
        record = {"session_id": "stream", "cwd": "/synthetic/project", "updated_at": "2026-08-27T14:00:00Z",
                  "messages": [{"id": "m", "role": "user", "content": "streamed"}]}
        payload = json.dumps(record)
        def runner(command, **kwargs):
            del command
            return subprocess.Popen([sys.executable, "-c", "import sys; print(sys.argv[1], flush=True)", payload], **kwargs)
        result = HermesAdapter(command_runner=runner).scan(ProjectConfig.create("synthetic", Path("/synthetic/project")), {})
        self.assertEqual([m.content for m in result.messages], ["streamed"])

    def test_scan_timeout_kills_hanging_synthetic_subprocess_without_output(self):
        def runner(command, **kwargs):
            del command
            return subprocess.Popen([sys.executable, "-c", "import time; print('first', flush=True); time.sleep(5)"], **kwargs)
        result = HermesAdapter(command_runner=runner, timeout=0.15).scan(
            ProjectConfig.create("synthetic", Path("/synthetic/project")), {})
        self.assertEqual(result.messages, ())
        self.assertTrue(any("timed out" in warning for warning in result.warnings))
        self.assertNotIn("first", " ".join(result.warnings))

    def test_scan_timeout_discards_partial_valid_sessions_and_preserves_state(self):
        record = {"session_id": "partial-timeout", "cwd": "/synthetic/project",
                  "updated_at": "2026-08-27T15:00:00Z",
                  "messages": [{"id": "m", "role": "user", "content": "partial"}]}
        payload = json.dumps(record)

        def runner(command, **kwargs):
            del command
            return subprocess.Popen([
                sys.executable, "-c",
                "import sys,time; print(sys.argv[1], flush=True); time.sleep(5)", payload,
            ], **kwargs)

        state = {"newer_than": "2026-08-27T00:00:00Z"}
        result = HermesAdapter(command_runner=runner, timeout=0.15).scan(
            ProjectConfig.create("synthetic", Path("/synthetic/project")), state)
        self.assertEqual(result.messages, ())
        self.assertEqual(result.next_state, state)
        self.assertEqual(result.sessions_seen, 0)
        self.assertTrue(any(warning == "Hermes exporter timed out" for warning in result.warnings))
        self.assertNotIn(payload, " ".join(result.warnings))

    def test_scan_nonzero_subprocess_discards_partial_valid_sessions_and_preserves_state(self):
        record = {"session_id": "partial-failure", "cwd": "/synthetic/project",
                  "updated_at": "2026-08-27T16:00:00Z",
                  "messages": [{"id": "m", "role": "user", "content": "partial"}]}
        payload = json.dumps(record)

        def runner(command, **kwargs):
            del command
            return subprocess.Popen([
                sys.executable, "-c",
                "import sys; print(sys.argv[1], flush=True); sys.exit(7)", payload,
            ], **kwargs)

        state = {"newer_than": "2026-08-27T00:00:00Z"}
        result = HermesAdapter(command_runner=runner).scan(
            ProjectConfig.create("synthetic", Path("/synthetic/project")), state)
        self.assertEqual(result.messages, ())
        self.assertEqual(result.next_state, state)
        self.assertEqual(result.sessions_seen, 0)
        self.assertEqual(result.warnings, ("Hermes exporter exit 7",))
        self.assertNotIn(payload, " ".join(result.warnings))


if __name__ == "__main__":
    unittest.main()
