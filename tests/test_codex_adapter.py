from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from project_memory.adapters.codex import CodexAdapter
from project_memory.config import ProjectConfig


class CodexAdapterTests(unittest.TestCase):
    def test_scans_matching_cwd_extracts_canonical_messages_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(__file__).parent / "fixtures" / "codex_session.jsonl"
            target = Path(tmp) / "session.jsonl"
            target.write_bytes(fixture.read_bytes())
            expected_offset = target.read_bytes().rfind(b"\n") + 1
            config = ProjectConfig.create("synthetic", Path("/synthetic/project"), [Path("/synthetic/alias")])
            result = CodexAdapter().scan(config, {"paths": [str(target)]})

        self.assertEqual(result.sessions_seen, 1)
        self.assertEqual([m.message_id for m in result.messages], ["r-user", "r-assistant", "r-second"])
        self.assertEqual(result.messages[0].content, "Keep the fixture synthetic.")
        self.assertEqual(result.messages[1].content, "Understood.")
        self.assertEqual(result.messages[2].content, "Plain answer")
        self.assertEqual(result.messages[0].project_id, config.project_id)
        self.assertTrue(all(message.role in {"user", "assistant"} for message in result.messages))
        self.assertEqual(result.next_state["files"][str(target.resolve())]["offset"], expected_offset)

    def test_ignores_partial_final_line_and_duplicate_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "partial.jsonl"
            complete = b'{"type":"session_meta","payload":{"id":"s","cwd":"/synthetic/project"}}\n'
            complete += b'{"type":"response_item","payload":{"id":"m","type":"message","role":"user","content":"hello"}}\n'
            partial = b'{"type":"response_item","payload":{"id":"tail","type":"message"}'
            target.write_bytes(complete + partial)
            config = ProjectConfig.create("synthetic", Path("/synthetic/project"))
            result = CodexAdapter().scan(config, {"paths": [str(target)]})
            self.assertEqual([m.message_id for m in result.messages], ["m"])
            self.assertEqual(result.next_state["files"][str(target.resolve())]["offset"], len(complete))

    def test_directory_source_scans_jsonl_recursively_and_preserves_known_file_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            nested = root / "nested"
            nested.mkdir(parents=True)
            first = nested / "b.jsonl"
            second = root / "a.jsonl"
            meta = {"type": "session_meta", "payload": {"id": "s", "cwd": "/synthetic/project", "timestamp": "t"}}
            first.write_text(json.dumps(meta) + "\n" + json.dumps({"type": "response_item", "payload": {"id": "m1", "type": "message", "role": "user", "content": "one"}}) + "\n")
            second.write_text(json.dumps(meta | {"payload": {**meta["payload"], "id": "s2"}}) + "\n" + json.dumps({"type": "response_item", "payload": {"id": "m2", "type": "message", "role": "assistant", "content": "two"}}) + "\n")
            config = ProjectConfig.create("synthetic", Path("/synthetic/project"))
            result = CodexAdapter().scan(config, {"paths": [str(root)]})
            self.assertEqual([m.message_id for m in result.messages], ["m2", "m1"])
            known = result.next_state["files"][str(first.resolve())]
            second_result = CodexAdapter().scan(config, result.next_state)
            self.assertEqual(second_result.next_state["files"][str(first.resolve())], known)

    def test_incremental_file_reuses_session_context_after_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.jsonl"
            target.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s", "cwd": "/synthetic/project"}}) + "\n")
            config = ProjectConfig.create("synthetic", Path("/synthetic/project"))
            state = CodexAdapter().scan(config, {"paths": [str(target)]}).next_state
            with target.open("a") as handle:
                handle.write(json.dumps({"type": "response_item", "payload": {"id": "new", "type": "message", "role": "user", "content": "later"}}) + "\n")
            result = CodexAdapter().scan(config, state)
            self.assertEqual([m.message_id for m in result.messages], ["new"])
            self.assertEqual(result.messages[0].session_id, "s")

    def test_deduplicates_by_session_and_message_id_not_message_id_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for session in ("s1", "s2"):
                path = Path(tmp) / f"{session}.jsonl"
                path.write_text(json.dumps({"type": "session_meta", "payload": {"id": session, "cwd": "/synthetic/project"}}) + "\n" + json.dumps({"type": "response_item", "payload": {"id": "same", "type": "message", "role": "user", "content": session}}) + "\n")
                paths.append(str(path))
            result = CodexAdapter().scan(ProjectConfig.create("synthetic", Path("/synthetic/project")), {"paths": paths})
            self.assertEqual([(m.session_id, m.message_id) for m in result.messages], [("s1", "same"), ("s2", "same")])

    def test_messages_use_session_meta_context_at_their_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "contexts.jsonl"
            records = [
                {"type": "session_meta", "payload": {"id": "good", "cwd": "/synthetic/project"}},
                {"type": "response_item", "payload": {"id": "good-msg", "type": "message", "role": "user", "content": "keep"}},
                {"type": "session_meta", "payload": {"id": "wrong", "cwd": "/other/project"}},
                {"type": "response_item", "payload": {"id": "wrong-msg", "type": "message", "role": "assistant", "content": "drop"}},
            ]
            target.write_text("\n".join(json.dumps(record) for record in records) + "\n")
            result = CodexAdapter().scan(ProjectConfig.create("synthetic", Path("/synthetic/project")), {"paths": [str(target)]})
            self.assertEqual([(m.session_id, m.message_id) for m in result.messages], [("good", "good-msg")])


if __name__ == "__main__":
    unittest.main()
