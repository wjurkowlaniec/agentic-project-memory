from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from project_memory.adapters.windsurf import WindsurfAdapter, capture_windsurf_event
from project_memory.config import ProjectConfig


class WindsurfAdapterTests(unittest.TestCase):
    def official_transcript(self, path: Path) -> None:
        records = [
            {"type": "user_input", "status": "done", "user_input": {"user_response": "User request."}, "timestamp": 1_700_000_001},
            {"type": "user_input", "status": "pending", "user_input": {"user_response": "Ignore pending."}, "timestamp": 1_700_000_002},
            {"type": "tool", "status": "done", "tool": {"name": "shell", "input": "DO NOT IMPORT TOOL"}, "timestamp": 1_700_000_003},
            {"type": "code", "status": "done", "code": {"code": "DO NOT IMPORT CODE"}, "timestamp": 1_700_000_004},
            {"type": "planner_response", "status": "done", "planner_response": {"response": "Assistant response."}, "timestamp": 1_700_000_005},
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def hook_event(self, transcript: Path) -> dict[str, object]:
        return {
            "agent_action_name": "post_cascade_response_with_transcript",
            "trajectory_id": "trajectory-1",
            "execution_id": "execution-1",
            "timestamp": 1_700_000_100,
            "tool_info": {"transcript_path": str(transcript)},
        }

    def test_capture_validates_root_and_writes_private_normalized_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            root = directory / "project"; root.mkdir()
            transcript_root = directory / "transcripts"; transcript_root.mkdir()
            transcript = transcript_root / "official.jsonl"
            self.official_transcript(transcript)
            inbox = directory / "inbox"
            event = self.hook_event(transcript)

            captured = capture_windsurf_event(event, transcript_root=transcript_root, inbox_dir=inbox)
            first_bytes = captured.read_bytes()
            repeat = capture_windsurf_event(event, transcript_root=transcript_root, inbox_dir=inbox)

            self.assertEqual(captured, repeat)
            self.assertEqual(repeat.read_bytes(), first_bytes)
            self.assertEqual(stat.S_IMODE(repeat.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(inbox.stat().st_mode), 0o700)
            normalized = [json.loads(line) for line in first_bytes.decode().splitlines()]
            self.assertTrue(normalized)
            self.assertTrue(all({"session_id", "message_id", "role", "timestamp", "content"} <= set(row) for row in normalized))
            self.assertNotIn("DO NOT IMPORT", first_bytes.decode())

    def test_adapter_scans_only_normalized_inbox_and_second_scan_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            root = directory / "project"; root.mkdir()
            transcript_root = directory / "transcripts"; transcript_root.mkdir()
            transcript = transcript_root / "official.jsonl"
            self.official_transcript(transcript)
            inbox = directory / "inbox"
            capture_windsurf_event(self.hook_event(transcript), transcript_root=transcript_root, inbox_dir=inbox)
            config = ProjectConfig.create("windsurf", root)
            adapter = WindsurfAdapter(inbox_dir=inbox)

            first = adapter.scan(config, {})
            second = adapter.scan(config, first.next_state)

        self.assertEqual([(message.role, message.content) for message in first.messages], [
            ("user", "User request."), ("assistant", "Assistant response.")])
        self.assertEqual({message.source for message in first.messages}, {"windsurf"})
        self.assertEqual(second.messages, ())
        self.assertEqual(second.next_state, first.next_state)

    def test_malformed_normalized_json_is_a_generic_batch_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"; inbox.mkdir(mode=0o700)
            (inbox / "capture.jsonl").write_text('{"role":"user","content":"secret"}\n{not json\n', encoding="utf-8")
            result = WindsurfAdapter(inbox_dir=inbox).scan(
                ProjectConfig.create("windsurf", Path(tmp) / "project"), {})

        self.assertEqual(result.messages, ())
        self.assertTrue(result.warnings)
        warning_text = " ".join(result.warnings).lower()
        self.assertNotIn("secret", warning_text)
        self.assertNotIn("capture.jsonl", warning_text)
        self.assertNotIn("not json", warning_text)

    def test_capture_rejects_hook_transcript_outside_injected_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            transcript_root = directory / "transcripts"; transcript_root.mkdir()
            outside = directory / "outside.jsonl"; outside.write_text("{}\n", encoding="utf-8")
            event = self.hook_event(outside)

            with self.assertRaisesRegex(ValueError, "transcript_path_outside_root"):
                capture_windsurf_event(event, transcript_root=transcript_root, inbox_dir=directory / "inbox")

    def test_capture_rejects_invalid_action_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            transcript_root = directory / "transcripts"; transcript_root.mkdir()
            outside = directory / "outside.jsonl"; outside.write_text("{}\n", encoding="utf-8")
            link = transcript_root / "linked.jsonl"
            link.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "invalid_agent_action_name"):
                capture_windsurf_event(
                    {**self.hook_event(outside), "agent_action_name": "unexpected"},
                    transcript_root=transcript_root, inbox_dir=directory / "inbox")
            with self.assertRaisesRegex(ValueError, "transcript_path_outside_root"):
                capture_windsurf_event(
                    self.hook_event(link), transcript_root=transcript_root, inbox_dir=directory / "inbox")


if __name__ == "__main__":
    unittest.main()
