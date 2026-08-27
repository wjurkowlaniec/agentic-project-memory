from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from project_memory import cli


class CaptureCliTests(unittest.TestCase):
    def test_capture_windsurf_parser_has_only_root_and_reads_event_from_stdin(self):
        args = cli._parser().parse_args(["capture-windsurf", "--root", "/tmp/project"])
        self.assertEqual(args.command, "capture-windsurf")
        self.assertEqual(args.root, "/tmp/project")
        self.assertFalse(hasattr(args, "source"))
        self.assertFalse(hasattr(args, "transcript"))

    def test_capture_requires_initialized_project_and_only_writes_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            root = directory / "project"; root.mkdir()
            transcript_root = directory / "transcripts"; transcript_root.mkdir()
            transcript = transcript_root / "official.jsonl"
            transcript_bytes = (json.dumps({
                "type": "user_input",
                "status": "done",
                "user_input": {"user_response": "Capture this."},
                "timestamp": 1_700_000_001,
            }) + "\n").encode()
            transcript.write_bytes(transcript_bytes)
            event = {
                "agent_action_name": "post_cascade_response_with_transcript",
                "trajectory_id": "trajectory-1",
                "execution_id": "execution-1",
                "timestamp": 1_700_000_100,
                "tool_info": {"transcript_path": str(transcript)},
            }
            data_home = directory / "data"
            env = {
                "PMEM_DATA_HOME": str(data_home),
                "PMEM_WINDSURF_TRANSCRIPTS_HOME": str(transcript_root),
            }
            with patch.dict(os.environ, env, clear=False):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["init", "--root", str(root), "--name", "capture-test"]), 0)
                with patch("sys.stdin", io.StringIO(json.dumps(event) + "\n")):
                    with redirect_stdout(io.StringIO()) as output:
                        result = cli.main(["capture-windsurf", "--root", str(root)])

            self.assertEqual(result, 0)
            self.assertIn("captured", output.getvalue().lower())
            project_dirs = list((data_home / "projects").glob("*/"))
            self.assertTrue(project_dirs)
            self.assertFalse(any(path.name in {"vault.sqlite3", "memory.sqlite3"} for path in project_dirs[0].iterdir()))
            source_dir = project_dirs[0] / "sources" / "windsurf"
            capture_files = list(source_dir.rglob("*.jsonl"))
            self.assertEqual(len(capture_files), 1)
            self.assertEqual(transcript.read_bytes(), transcript_bytes)
            self.assertTrue(all(path.is_file() and path.parent == source_dir for path in capture_files))
            self.assertEqual(list(transcript_root.rglob("*")), [transcript])

    def test_capture_rejects_uninitialized_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"; root.mkdir()
            with patch.dict(os.environ, {"PMEM_DATA_HOME": str(Path(tmp) / "data")}, clear=False):
                with patch("sys.stdin", io.StringIO("{}\n")):
                    self.assertEqual(cli.main(["capture-windsurf", "--root", str(root)]), 2)


if __name__ == "__main__":
    unittest.main()
