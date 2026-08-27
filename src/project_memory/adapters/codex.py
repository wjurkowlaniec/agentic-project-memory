from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import ProjectConfig
from ..models import NormalizedCommand, NormalizedMessage
from .base import SyncBatch


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in {"input_text", "output_text", "text"}:
            return str(value.get("text", value.get("content", "")))
    return ""


def _canonical(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _within_project(path: str | Path, members: set[Path]) -> bool:
    candidate = _canonical(path)
    return any(candidate == member or member in candidate.parents for member in members)


def _source_files(raw_paths: object) -> list[Path]:
    values = [raw_paths] if isinstance(raw_paths, str) else list(raw_paths or [])
    result: set[Path] = set()
    for value in values:
        path = _canonical(value)
        if path.is_dir():
            result.update(p for p in path.rglob("*.jsonl") if p.is_file())
        elif path.is_file():
            result.add(path)
    return sorted(result, key=str)


class CodexAdapter:
    source = "codex"

    def scan(self, config: ProjectConfig, state: dict[str, object]) -> SyncBatch:
        raw_paths = state.get("paths") or config.source_locations.get("codex", "")
        paths = _source_files(raw_paths)
        files_state = dict(state.get("files", {}))
        messages: list[NormalizedMessage] = []
        commands: list[NormalizedCommand] = []
        seen_ids: set[tuple[str, str]] = set()
        sessions: set[str] = set()
        next_files: dict[str, object] = dict(files_state)
        members = {config.root, *config.aliases}
        command_backfill = state.get("command_index_version") != 1
        for path in paths:
            data = path.read_bytes()
            path_key = str(path)
            old = dict(files_state.get(path_key, {}))
            offset = int(old.get("offset", 0))
            old_hash = old.get("rolling_hash")
            if offset > len(data) or (old_hash and hashlib.sha256(data[:offset]).hexdigest() != old_hash):
                offset = 0
                old = {}
            chunk = data[offset:]
            complete_end = len(chunk) if chunk.endswith(b"\n") else chunk.rfind(b"\n") + 1
            complete = chunk[:complete_end]
            stat = path.stat()
            cwd: str | None = old.get("cwd") or None
            session_id = str(old.get("session_id", path.stem))
            timestamp = str(old.get("timestamp", ""))
            next_files[path_key] = {
                "path": path_key, "size": len(data), "mtime_ns": stat.st_mtime_ns,
                "offset": offset + complete_end,
                "rolling_hash": hashlib.sha256(data[:offset + complete_end]).hexdigest(),
                "cwd": cwd, "session_id": session_id, "timestamp": timestamp,
            }
            pending: list[tuple[dict[str, Any], str | None, str, str]] = []
            for line in complete.splitlines():
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                payload = record.get("payload", record)
                if record.get("type") == "session_meta" or payload.get("type") == "session_meta":
                    meta = payload.get("payload", payload)
                    cwd = str(meta.get("cwd", cwd or ""))
                    session_id = str(meta.get("id", meta.get("session_id", session_id)))
                    timestamp = str(meta.get("timestamp", meta.get("created_at", timestamp)))
                    continue
                if record.get("type") != "response_item":
                    continue
                if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
                    continue
                pending.append((payload, cwd, session_id, timestamp))
            next_files[path_key].update({"cwd": cwd, "session_id": session_id, "timestamp": timestamp})
            command_data = data if command_backfill else complete
            command_end = len(command_data) if command_data.endswith(b"\n") else command_data.rfind(b"\n") + 1
            command_cwd: str | None = None if command_backfill else (old.get("cwd") or None)
            command_session = path.stem if command_backfill else str(old.get("session_id", path.stem))
            command_timestamp = "" if command_backfill else str(old.get("timestamp", ""))
            pending_commands: list[tuple[dict[str, Any], str | None, str, str]] = []
            for line in command_data[:command_end].splitlines():
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                payload = record.get("payload", record)
                if record.get("type") == "session_meta" or payload.get("type") == "session_meta":
                    meta = payload.get("payload", payload)
                    command_cwd = str(meta.get("cwd", command_cwd or ""))
                    command_session = str(meta.get("id", meta.get("session_id", command_session)))
                    command_timestamp = str(meta.get("timestamp", meta.get("created_at", command_timestamp)))
                    continue
                if record.get("type") == "response_item" and payload.get("type") == "function_call" and payload.get("name") == "exec_command":
                    pending_commands.append((payload, command_cwd, command_session, str(record.get("timestamp", payload.get("timestamp", command_timestamp)))))
            for payload, command_cwd, command_session, command_timestamp in pending_commands:
                if command_cwd is None or _canonical(command_cwd) not in members:
                    continue
                arguments = payload.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        continue
                if not isinstance(arguments, dict) or not isinstance(arguments.get("cmd"), str):
                    continue
                workdir = str(arguments.get("workdir") or command_cwd)
                if not _within_project(workdir, members):
                    continue
                command = arguments["cmd"].strip()
                command_id = str(payload.get("call_id") or payload.get("id") or "")
                if not command or not command_id:
                    continue
                digest = hashlib.sha256(f"{command}\0{workdir}".encode("utf-8")).hexdigest()
                commands.append(NormalizedCommand(
                    self.source, command_session, command_id, config.project_id,
                    command_timestamp, command, workdir, digest,
                ))
                sessions.add(command_session)
            for payload, message_cwd, message_session, message_timestamp in pending:
                if message_cwd is None or _canonical(message_cwd) not in members:
                    continue
                sessions.add(message_session)
                message_id = str(payload.get("id", ""))
                key = (message_session, message_id)
                if not message_id or key in seen_ids:
                    continue
                content = _text(payload.get("content", payload.get("message", "")))
                if not content:
                    continue
                seen_ids.add(key)
                messages.append(NormalizedMessage(
                    self.source, message_session, message_id, config.project_id,
                    str(payload["role"]), str(payload.get("timestamp", message_timestamp)), content,
                    path_key, hashlib.sha256(content.encode("utf-8")).hexdigest(), {}))
        next_state = dict(state)
        next_state["files"] = next_files
        next_state["command_index_version"] = 1
        return SyncBatch(len(sessions), tuple(messages), next_state, (), tuple(commands))
