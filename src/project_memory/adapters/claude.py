from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from ..config import ProjectConfig
from ..discovery import claude_project_dir
from ..models import NormalizedCommand, NormalizedMessage
from .base import SyncBatch


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    if isinstance(value, dict) and value.get("type") in {"text", "input_text", "output_text"}:
        return str(value.get("text", value.get("content", "")))
    return ""


class ClaudeAdapter:
    source = "claude"

    def __init__(self, session_dirs: Iterable[str | Path] | None = None):
        self.session_dirs = tuple(Path(path).expanduser() for path in session_dirs) if session_dirs is not None else None

    def _directories(self, config: ProjectConfig) -> tuple[Path, ...]:
        if self.session_dirs is not None:
            return self.session_dirs
        configured = config.source_locations.get(self.source)
        if configured:
            return (Path(configured).expanduser(),)
        return tuple(claude_project_dir(member) for member in (config.root, *config.aliases))

    def scan(self, config: ProjectConfig, state: dict[str, object]) -> SyncBatch:
        files = dict(state.get("files", {}))
        messages: list[NormalizedMessage] = []
        commands: list[NormalizedCommand] = []
        sessions: set[str] = set()
        warnings = False
        members = {config.root, *config.aliases}
        command_backfill = state.get("command_index_version") != 1
        for directory in self._directories(config):
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*.jsonl")):
                if not path.is_file() or path.is_symlink():
                    continue
                key = str(path.resolve())
                try:
                    data = path.read_bytes()
                except OSError:
                    warnings = True
                    continue
                digest = hashlib.sha256(data).hexdigest()
                file_already_seen = files.get(key) == digest
                if file_already_seen and not command_backfill:
                    continue
                for index, raw in enumerate(data.decode("utf-8", errors="replace").splitlines()):
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        warnings = True
                        continue
                    if not isinstance(record, dict) or record.get("type") not in {"user", "assistant"}:
                        continue
                    message = record.get("message")
                    if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                        warnings = True
                        continue
                    cwd = record.get("cwd")
                    if isinstance(cwd, str) and Path(cwd).expanduser().resolve(strict=False) not in members:
                        continue
                    session_id = str(record.get("sessionId") or record.get("session_id") or path.stem)
                    timestamp = str(record.get("timestamp", ""))
                    if message.get("role") == "assistant" and isinstance(message.get("content"), list):
                        for block_index, block in enumerate(message["content"]):
                            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Bash":
                                continue
                            arguments = block.get("input", {})
                            if not isinstance(arguments, dict) or not isinstance(arguments.get("command"), str):
                                continue
                            base_cwd = Path(cwd).expanduser().resolve(strict=False) if isinstance(cwd, str) else config.root
                            workdir = Path(str(arguments.get("workdir") or base_cwd)).expanduser().resolve(strict=False)
                            if not any(workdir == member or member in workdir.parents for member in members):
                                continue
                            command = arguments["command"].strip()
                            command_id = str(block.get("id") or f"{session_id}:{index}:{block_index}")
                            if command and command_id:
                                commands.append(NormalizedCommand(
                                    self.source, session_id, command_id, config.project_id, timestamp,
                                    command, str(workdir), hashlib.sha256(f"{command}\0{workdir}".encode("utf-8")).hexdigest(),
                                ))
                                sessions.add(session_id)
                    if file_already_seen:
                        continue
                    content = _text(message.get("content"))
                    if not content:
                        warnings = True
                        continue
                    message_id = f"{session_id}:{index}"
                    sessions.add(session_id)
                    messages.append(NormalizedMessage(
                        self.source, session_id, message_id, config.project_id, str(message["role"]),
                        timestamp, content, None,
                        hashlib.sha256(content.encode("utf-8")).hexdigest(), {},
                    ))
                files[key] = digest
        next_state = dict(state)
        next_state["files"] = files
        next_state["command_index_version"] = 1
        return SyncBatch(len(sessions), tuple(messages), next_state, ("claude: source data partially unreadable",) if warnings else (), tuple(commands))
