from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import ProjectConfig
from ..discovery import devin_database
from ..models import NormalizedMessage
from .base import SyncBatch

_DEFAULT_DB = devin_database()
_SAFE_SESSION_FIELDS = ("backend_type", "model", "agent_mode", "title")


def _canonical(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    return Path(value).expanduser().resolve(strict=False)


def _timestamp(value: object) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _snapshot(source: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix="pmem-devin-", suffix=".sqlite3")
    os.close(fd)
    target = Path(name)
    try:
        src = sqlite3.connect(f"file:{source.expanduser()}?mode=ro", uri=True)
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


class DevinAdapter:
    source = "devin"

    def __init__(self, database: str | Path | None = None):
        self.database = Path(database).expanduser() if database else None

    def scan(self, config: ProjectConfig, state: dict[str, object]) -> SyncBatch:
        database = self.database or Path(config.source_locations.get(self.source, _DEFAULT_DB)).expanduser()
        if not database.is_file():
            return SyncBatch(0, (), dict(state), ())
        snapshot = _snapshot(database)
        try:
            return self._scan_snapshot(snapshot, config, state)
        finally:
            snapshot.unlink(missing_ok=True)

    def _scan_snapshot(self, snapshot: Path, config: ProjectConfig, state: dict[str, object]) -> SyncBatch:
        members = {config.root, *config.aliases}
        imported = {str(x) for x in state.get("messages", []) if isinstance(x, str)}
        messages: list[NormalizedMessage] = []
        sessions = 0
        warnings = False
        db = sqlite3.connect(snapshot); db.row_factory = sqlite3.Row
        try:
            session_rows = db.execute("SELECT * FROM sessions").fetchall()
            matched: dict[str, sqlite3.Row] = {}
            for row in session_rows:
                locations = [_canonical(row["working_directory"])]
                try:
                    parsed = json.loads(row["workspace_dirs"] or "[]")
                    if isinstance(parsed, list): locations.extend(_canonical(p) for p in parsed)
                    else: warnings = True
                except (TypeError, ValueError, json.JSONDecodeError):
                    warnings = True
                if any(location in members for location in locations if location is not None):
                    matched[str(row["id"])] = row
            if not matched:
                next_state = dict(state); return SyncBatch(0, (), next_state, ("devin: source data partially unreadable",) if warnings else ())
            placeholders = ",".join("?" for _ in matched)
            rows = db.execute(f"SELECT * FROM message_nodes WHERE session_id IN ({placeholders}) ORDER BY row_id", tuple(matched)).fetchall()
            for row in rows:
                key = f"{row['session_id']}:{row['node_id']}"
                try:
                    payload = json.loads(row["chat_message"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    warnings = True; continue
                if not isinstance(payload, dict) or payload.get("role") not in {"user", "assistant"} or not isinstance(payload.get("content"), str) or not payload["content"]:
                    warnings = True; continue
                if key in imported: continue
                session = matched[str(row["session_id"])]
                metadata = {field: session[field] for field in _SAFE_SESSION_FIELDS if field in session.keys() and session[field] is not None}
                messages.append(NormalizedMessage(self.source, str(row["session_id"]), str(row["node_id"]), config.project_id,
                    payload["role"], _timestamp(row["created_at"]), payload["content"], None, hashlib.sha256(payload["content"].encode("utf-8")).hexdigest(), metadata))
                imported.add(key)
        finally:
            db.close()
        next_state = dict(state); next_state["messages"] = sorted(imported)
        sessions = len({message.session_id for message in messages})
        return SyncBatch(sessions, tuple(messages), next_state, ("devin: source data partially unreadable",) if warnings else ())
