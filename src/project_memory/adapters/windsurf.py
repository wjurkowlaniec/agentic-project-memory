from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import ProjectConfig
from ..models import NormalizedMessage
from .base import SyncBatch

ACTION = "post_cascade_response_with_transcript"
_DEFAULT_TRANSCRIPTS = Path.home() / ".windsurf/transcripts"


def _iso(value: object) -> str:
    try: return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError): return ""


def _safe_name(value: object) -> str:
    text = str(value) if isinstance(value, str) and value else "trajectory"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)[:100] or "trajectory"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700); path.parent.chmod(0o700)
    fd, temp = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path); path.chmod(0o600)
    finally:
        Path(temp).unlink(missing_ok=True)


def capture_windsurf_event(event: dict[str, Any], *, transcript_root: str | Path | None = None, inbox_dir: str | Path) -> Path:
    if event.get("agent_action_name") != ACTION: raise ValueError("invalid_agent_action_name")
    root = Path(transcript_root or _DEFAULT_TRANSCRIPTS).expanduser().resolve()
    raw = ((event.get("tool_info") or {}).get("transcript_path"))
    if not isinstance(raw, str): raise ValueError("missing_transcript_path")
    candidate = Path(raw).expanduser()
    try: resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError): raise ValueError("transcript_path_outside_root")
    if root not in (resolved, *resolved.parents) or not candidate.is_file() or candidate.is_symlink():
        raise ValueError("transcript_path_outside_root")
    if not candidate.is_file() or candidate.is_symlink(): raise ValueError("invalid_transcript_file")
    normalized: list[dict[str, object]] = []
    try:
        for line in candidate.read_text(encoding="utf-8").splitlines():
            try: record = json.loads(line)
            except (ValueError, UnicodeDecodeError): continue
            if not isinstance(record, dict) or record.get("status") != "done": continue
            typ = record.get("type"); payload = record.get("user_input") if typ == "user_input" else record.get("planner_response") if typ == "planner_response" else None
            key = "user_response" if typ == "user_input" else "response"
            if not isinstance(payload, dict) or not isinstance(payload.get(key), str) or not payload[key]: continue
            normalized.append({"session_id": str(event.get("trajectory_id") or "trajectory"), "message_id": hashlib.sha256((str(event.get("trajectory_id")) + ":" + str(len(normalized))).encode()).hexdigest()[:24], "role": "user" if typ == "user_input" else "assistant", "timestamp": _iso(record.get("timestamp", event.get("timestamp"))), "content": payload[key]})
    except (OSError, UnicodeError): raise ValueError("transcript_unreadable")
    inbox = Path(inbox_dir).expanduser().resolve()
    target = inbox / (_safe_name(event.get("trajectory_id")) + ".jsonl")
    data = b"".join(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n" for row in normalized)
    _atomic_write(target, data)
    return target


class WindsurfAdapter:
    source = "windsurf"
    def __init__(self, inbox_dir: str | Path | None = None): self.inbox_dir = Path(inbox_dir or "").expanduser() if inbox_dir else None
    def scan(self, config: ProjectConfig, state: dict[str, object]) -> SyncBatch:
        inbox = (self.inbox_dir or Path(config.source_locations.get(self.source, ""))).expanduser()
        if not inbox.is_dir(): return SyncBatch(0, (), dict(state), ())
        files = dict(state.get("files", {})); messages=[]; sessions=set(); warnings=False
        for path in sorted(inbox.glob("*.jsonl")):
            if not path.is_file() or path.is_symlink(): continue
            key=str(path.resolve()); digest=hashlib.sha256(path.read_bytes()).hexdigest()
            if files.get(key)==digest: continue
            try: rows=path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError): warnings=True; continue
            for line in rows:
                try: row=json.loads(line)
                except (ValueError, UnicodeDecodeError): warnings=True; continue
                if not isinstance(row,dict) or not all(isinstance(row.get(k),str) and row.get(k) for k in ("session_id","message_id","role","timestamp","content")) or row["role"] not in {"user","assistant"}:
                    warnings=True; continue
                sessions.add(row["session_id"]); messages.append(NormalizedMessage(self.source,row["session_id"],row["message_id"],config.project_id,row["role"],row["timestamp"],row["content"],None,hashlib.sha256(row["content"].encode()).hexdigest(),{}))
            files[key]=digest
        next_state=dict(state); next_state["files"]=files
        return SyncBatch(len(sessions),tuple(messages),next_state,("windsurf: inbox data partially unreadable",) if warnings else ())
