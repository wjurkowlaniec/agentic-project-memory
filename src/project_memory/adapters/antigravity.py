from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..config import ProjectConfig
from ..models import NormalizedCommand, NormalizedMessage
from .base import SyncBatch


_COMMAND_TOOLS = {"command", "execute_command", "run_command", "terminal"}


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid_protobuf_varint")


def _protobuf_strings(data: bytes, *, depth: int = 0) -> set[str]:
    """Extract complete UTF-8 protobuf string fields without schema guessing."""
    if depth > 10:
        return set()
    result: set[str] = set()
    offset = 0
    try:
        while offset < len(data):
            key, offset = _varint(data, offset)
            field_number, wire_type = key >> 3, key & 7
            if field_number == 0:
                raise ValueError("invalid_protobuf_field")
            if wire_type == 0:
                _, offset = _varint(data, offset)
            elif wire_type == 1:
                offset += 8
            elif wire_type == 5:
                offset += 4
            elif wire_type == 2:
                length, offset = _varint(data, offset)
                payload = data[offset:offset + length]
                offset += length
                if len(payload) != length:
                    raise ValueError("truncated_protobuf_field")
                try:
                    text = payload.decode("utf-8")
                    if text and all(char in "\n\r\t" or ord(char) >= 32 for char in text):
                        result.add(text)
                except UnicodeDecodeError:
                    pass
                result.update(_protobuf_strings(payload, depth=depth + 1))
            else:
                raise ValueError("unsupported_protobuf_wire_type")
        if offset != len(data):
            raise ValueError("trailing_protobuf_data")
    except (ValueError, IndexError):
        return set()
    return result


def _canonical_metadata_path(value: str) -> Path | None:
    if value.startswith("file://"):
        parsed = urlparse(value)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            return None
        value = unquote(parsed.path)
    elif not value.startswith("/"):
        return None
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _snapshot(source: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix="pmem-antigravity-", suffix=".sqlite3")
    os.close(fd)
    target = Path(name)
    try:
        original = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        copy = sqlite3.connect(target)
        with copy:
            original.backup(copy)
        original.close(); copy.close()
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


class AntigravityAdapter:
    source = "antigravity"

    def __init__(self, conversations_dir: str | Path | None = None,
                 brain_dir: str | Path | None = None):
        self.conversations_dir = Path(conversations_dir).expanduser() if conversations_dir else None
        self.brain_dir = Path(brain_dir).expanduser() if brain_dir else None

    def _locations(self, config: ProjectConfig) -> tuple[Path, Path]:
        configured = config.source_locations.get(self.source)
        if self.conversations_dir is not None:
            conversations = self.conversations_dir
        elif configured:
            conversations = Path(configured).expanduser()
        else:
            conversations = Path.home() / ".gemini" / "antigravity" / "conversations"
        brain = self.brain_dir or conversations.parent / "brain"
        return conversations, brain

    def _matches_project(self, database: Path, members: set[Path]) -> bool:
        snapshot = _snapshot(database)
        try:
            db = sqlite3.connect(snapshot)
            try:
                rows = db.execute("SELECT data FROM trajectory_metadata_blob").fetchall()
            finally:
                db.close()
        finally:
            snapshot.unlink(missing_ok=True)
        paths = {
            path for row in rows for value in _protobuf_strings(bytes(row[0] or b""))
            if (path := _canonical_metadata_path(value)) is not None
        }
        return bool(paths & members)

    def scan(self, config: ProjectConfig, state: dict[str, object]) -> SyncBatch:
        conversations, brain = self._locations(config)
        if not conversations.is_dir():
            return SyncBatch(0, (), dict(state), ())
        members = {config.root, *config.aliases}
        files = dict(state.get("files", {}))
        next_files = dict(files)
        messages: list[NormalizedMessage] = []
        commands: list[NormalizedCommand] = []
        sessions: set[str] = set()
        warning = False
        for database in sorted(conversations.glob("*.db")):
            if not database.is_file() or database.is_symlink():
                continue
            try:
                if not self._matches_project(database, members):
                    continue
            except (OSError, sqlite3.Error, ValueError, TypeError):
                warning = True
                continue
            session_id = database.stem
            logs = brain / session_id / ".system_generated" / "logs"
            transcript = logs / "transcript_full.jsonl"
            if not transcript.is_file() or transcript.is_symlink():
                transcript = logs / "transcript.jsonl"
            if not transcript.is_file() or transcript.is_symlink():
                continue
            try:
                data = transcript.read_bytes()
            except OSError:
                warning = True
                continue
            key = str(transcript.resolve())
            digest = hashlib.sha256(data).hexdigest()
            if files.get(key) == digest:
                continue
            for line in data.decode("utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    warning = True
                    continue
                if not isinstance(row, dict) or row.get("status") != "DONE":
                    continue
                row_type = row.get("type")
                timestamp = str(row.get("created_at", ""))
                step = str(row.get("step_index", ""))
                content = row.get("content")
                if row_type in {"USER_INPUT", "PLANNER_RESPONSE"} and isinstance(content, str) and content:
                    role = "user" if row_type == "USER_INPUT" else "assistant"
                    message_id = f"{step}:{row_type}"
                    messages.append(NormalizedMessage(
                        self.source, session_id, message_id, config.project_id, role,
                        timestamp, content, None, hashlib.sha256(content.encode("utf-8")).hexdigest(), {},
                    ))
                    sessions.add(session_id)
                for index, call in enumerate(row.get("tool_calls", [])):
                    if not isinstance(call, dict) or str(call.get("name", "")).lower() not in _COMMAND_TOOLS:
                        continue
                    arguments = call.get("args", call.get("arguments", {}))
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            continue
                    if not isinstance(arguments, dict):
                        continue
                    command = arguments.get("command", arguments.get("cmd"))
                    if not isinstance(command, str) or not command.strip():
                        continue
                    cwd_value = arguments.get("cwd", arguments.get("workdir", config.root))
                    try:
                        cwd = Path(str(cwd_value)).expanduser().resolve(strict=False)
                    except (OSError, RuntimeError):
                        continue
                    if not any(cwd == member or member in cwd.parents for member in members):
                        continue
                    command_id = str(call.get("id") or f"{step}:{index}")
                    clean = command.strip()
                    commands.append(NormalizedCommand(
                        self.source, session_id, command_id, config.project_id, timestamp,
                        clean, str(cwd), hashlib.sha256(f"{clean}\0{cwd}".encode("utf-8")).hexdigest(),
                    ))
                    sessions.add(session_id)
            next_files[key] = digest
        next_state = dict(state)
        next_state["files"] = next_files
        warnings = ("antigravity: source data partially unreadable",) if warning else ()
        return SyncBatch(len(sessions), tuple(messages), next_state, warnings, tuple(commands))
