from __future__ import annotations

import base64
import binascii
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .models import NormalizedMessage


def message_object_key(source: str, session_id: str, message_id: str) -> str:
    payload = json.dumps([source, session_id, message_id], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "message:" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def parse_message_object_key(value: str) -> tuple[str, str, str]:
    if not isinstance(value, str) or not value.startswith("message:"):
        raise ValueError("invalid_message_object_key")
    encoded = value[8:]
    try:
        parts = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("invalid_message_object_key") from exc
    if not isinstance(parts, list) or len(parts) != 3 or not all(isinstance(x, str) for x in parts) or message_object_key(*parts) != value:
        raise ValueError("invalid_message_object_key")
    return parts[0], parts[1], parts[2]


class _Repository:
    def __init__(self, path: Path, *, derived: bool) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        if derived:
            self.connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self.path.chmod(0o600)

    def _migrate(self) -> None: raise NotImplementedError
    def table_names(self) -> tuple[str, ...]:
        return tuple(row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall())
    def count_messages(self) -> int: return int(self.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    def close(self) -> None: self.connection.close()

    def _insert_message(self, message: NormalizedMessage, *, unique: str) -> None:
        metadata = json.dumps(message.metadata, sort_keys=True, separators=(", ", ": "))
        self.connection.execute(f"""INSERT INTO messages
            (source, session_id, message_id, project_id, role, timestamp, content, source_path, source_hash, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT {unique} DO UPDATE SET project_id=excluded.project_id, role=excluded.role,
            timestamp=excluded.timestamp, content=excluded.content, source_path=excluded.source_path,
            source_hash=excluded.source_hash, metadata_json=excluded.metadata_json""",
            (message.source, message.session_id, message.message_id, message.project_id, message.role,
             message.timestamp, message.content, message.source_path, message.source_hash, metadata))
        self.connection.commit()


class VaultRepository(_Repository):
    def __init__(self, path: Path) -> None: super().__init__(Path(getattr(path, "vault_db", path)), derived=False)
    def _migrate(self) -> None:
        self.connection.executescript("""CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, source TEXT NOT NULL, session_id TEXT NOT NULL,
        message_id TEXT NOT NULL, project_id TEXT NOT NULL, role TEXT NOT NULL, timestamp TEXT NOT NULL,
        content TEXT NOT NULL, source_path TEXT, source_hash TEXT NOT NULL, metadata_json TEXT NOT NULL,
        UNIQUE(source, session_id, message_id, source_hash));""")
        if self.connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
            self.connection.execute("INSERT INTO schema_version VALUES (1)"); self.connection.commit()
    def upsert_message(self, message: NormalizedMessage) -> None: self._insert_message(message, unique="(source, session_id, message_id, source_hash)")
    def latest(self, project_id: str, source: str, session_id: str, message_id: str):
        return self.connection.execute("SELECT * FROM messages WHERE project_id=? AND source=? AND session_id=? AND message_id=? ORDER BY id DESC LIMIT 1", (project_id, source, session_id, message_id)).fetchone()


class MemoryRepository(_Repository):
    def __init__(self, path: Path) -> None: super().__init__(Path(getattr(path, "memory_db", path)), derived=True)
    def _migrate(self) -> None:
        self.connection.executescript("""CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, source TEXT NOT NULL, session_id TEXT NOT NULL,
        message_id TEXT NOT NULL, object_key TEXT, project_id TEXT NOT NULL, role TEXT NOT NULL, timestamp TEXT NOT NULL,
        content TEXT NOT NULL, source_path TEXT, source_hash TEXT NOT NULL, metadata_json TEXT NOT NULL,
        UNIQUE(source, session_id, message_id));
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, object_key UNINDEXED, source, session_id, message_id, project_id);
        CREATE TABLE IF NOT EXISTS knowledge_items (item_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
        statement TEXT NOT NULL, state TEXT NOT NULL, evidence_message_id TEXT NOT NULL, evidence_source TEXT,
        evidence_session_id TEXT, evidence_quote TEXT NOT NULL, timestamp TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(item_id UNINDEXED, project_id UNINDEXED, statement);
        CREATE TABLE IF NOT EXISTS embeddings (object_type TEXT NOT NULL, object_id TEXT NOT NULL, project_id TEXT NOT NULL,
        model TEXT NOT NULL, dimension INTEGER NOT NULL, text_hash TEXT NOT NULL, vector BLOB NOT NULL,
        PRIMARY KEY(object_type, object_id, project_id, model));
        CREATE TABLE IF NOT EXISTS embedding_models (model TEXT PRIMARY KEY, dimension INTEGER NOT NULL);""")
        version = self.connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
        if version < 2: self._migrate_v1_to_v2()
        self._migrate_v2_to_v3()

    def _migrate_v1_to_v2(self) -> None:
        columns = {r[1] for r in self.connection.execute("PRAGMA table_info(messages)")}
        if "object_key" not in columns: self.connection.execute("ALTER TABLE messages ADD COLUMN object_key TEXT")
        for r in self.connection.execute("SELECT id,source,session_id,message_id FROM messages").fetchall(): self.connection.execute("UPDATE messages SET object_key=? WHERE id=?", (message_object_key(r[1],r[2],r[3]),r[0]))
        columns = {r[1] for r in self.connection.execute("PRAGMA table_info(knowledge_items)")}
        for name in ("evidence_source", "evidence_session_id"):
            if name not in columns: self.connection.execute(f"ALTER TABLE knowledge_items ADD COLUMN {name} TEXT")
        self.connection.execute("DROP TABLE messages_fts")
        self.connection.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content, object_key UNINDEXED, source, session_id, message_id, project_id)")
        self.connection.execute("INSERT INTO messages_fts(rowid,content,object_key,source,session_id,message_id,project_id) SELECT id,content,object_key,source,session_id,message_id,project_id FROM messages")
        self.connection.execute("DELETE FROM schema_version"); self.connection.execute("INSERT INTO schema_version VALUES (2)"); self.connection.commit()

    def _migrate_v2_to_v3(self) -> None:
        self.connection.executescript("""CREATE TABLE IF NOT EXISTS sync_state (source TEXT PRIMARY KEY, state_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS extraction_jobs (job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source TEXT NOT NULL, session_id TEXT NOT NULL, message_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, error_code TEXT, extraction_version INTEGER NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS item_links (source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL, project_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(source_id,target_id,relation,project_id));
        CREATE TABLE IF NOT EXISTS preflight_receipts (receipt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, query_hash TEXT NOT NULL, timestamp TEXT NOT NULL, receipt_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS suppression_log (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, project_id TEXT NOT NULL, reason TEXT NOT NULL, timestamp TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS embedding_jobs (job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, error_code TEXT, updated_at TEXT NOT NULL);""")
        columns = {r[1] for r in self.connection.execute("PRAGMA table_info(knowledge_items)")}
        for name, definition in {"confidence":"REAL NOT NULL DEFAULT 0", "evidence_start":"INTEGER NOT NULL DEFAULT 0", "evidence_end":"INTEGER NOT NULL DEFAULT 0", "extraction_version":"INTEGER NOT NULL DEFAULT 1"}.items():
            if name not in columns: self.connection.execute(f"ALTER TABLE knowledge_items ADD COLUMN {name} {definition}")
        self.connection.execute("DELETE FROM schema_version"); self.connection.execute("INSERT INTO schema_version VALUES (3)"); self.connection.commit()

    def upsert_message(self, message: NormalizedMessage) -> None:
        self._insert_message(message, unique="(source, session_id, message_id)")
        row = self.connection.execute("SELECT id FROM messages WHERE source=? AND session_id=? AND message_id=?", (message.source,message.session_id,message.message_id)).fetchone(); key = message_object_key(message.source,message.session_id,message.message_id)
        self.connection.execute("UPDATE messages SET object_key=? WHERE id=?", (key,row[0])); self.connection.execute("DELETE FROM messages_fts WHERE rowid=?", (row[0],)); self.connection.execute("INSERT INTO messages_fts(rowid,content,object_key,source,session_id,message_id,project_id) VALUES (?,?,?,?,?,?,?)", (row[0],message.content,key,message.source,message.session_id,message.message_id,message.project_id)); self.connection.commit()

    def upsert_knowledge_item(self, item_id, project_id, kind, statement, state, evidence_message_id, evidence_quote, timestamp, *, evidence_source=None, evidence_session_id=None):
        if evidence_source is None and evidence_session_id is None:
            matches=self.connection.execute("SELECT source,session_id FROM messages WHERE project_id=? AND message_id=?",(project_id,evidence_message_id)).fetchall()
            if len(matches)!=1: raise ValueError("knowledge evidence is unresolved")
            evidence_source,evidence_session_id=matches[0]
        elif evidence_source is None or evidence_session_id is None: raise ValueError("knowledge evidence is unresolved")
        if self.connection.execute("SELECT 1 FROM messages WHERE project_id=? AND source=? AND session_id=? AND message_id=?", (project_id,evidence_source,evidence_session_id,evidence_message_id)).fetchone() is None: raise ValueError("knowledge evidence is unresolved")
        self.connection.execute("""INSERT INTO knowledge_items(item_id,project_id,kind,statement,state,evidence_message_id,evidence_source,evidence_session_id,evidence_quote,timestamp) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET statement=excluded.statement,state=excluded.state,evidence_message_id=excluded.evidence_message_id,evidence_source=excluded.evidence_source,evidence_session_id=excluded.evidence_session_id,evidence_quote=excluded.evidence_quote,timestamp=excluded.timestamp""", (item_id,project_id,kind,statement,state,evidence_message_id,evidence_source,evidence_session_id,evidence_quote,timestamp)); self.connection.execute("DELETE FROM knowledge_fts WHERE item_id=?",(item_id,)); self.connection.execute("INSERT INTO knowledge_fts(item_id,project_id,statement) VALUES (?,?,?)",(item_id,project_id,statement)); self.connection.commit()

    def add_knowledge_candidate(self, project_id, item_id, kind, statement, state, confidence, evidence_source, evidence_session_id, evidence_message_id, evidence_quote, evidence_start, evidence_end, conflicts, supersedes, extraction_version, timestamp="now"):
        self.upsert_knowledge_item(item_id,project_id,kind,statement,state,evidence_message_id,evidence_quote,timestamp,evidence_source=evidence_source,evidence_session_id=evidence_session_id)
        self.connection.execute("UPDATE knowledge_items SET confidence=?,evidence_start=?,evidence_end=?,extraction_version=? WHERE item_id=?",(confidence,evidence_start,evidence_end,extraction_version,item_id))
        for target in conflicts: self.add_item_link(item_id,target,"conflicts_with",project_id,timestamp)
        for target in supersedes: self.add_item_link(item_id,target,"supersedes",project_id,timestamp)
        self.connection.commit()

    def add_item_link(self, source_id,target_id,relation,project_id,created_at="now"): self.connection.execute("INSERT OR IGNORE INTO item_links VALUES (?,?,?,?,?)",(source_id,target_id,relation,project_id,created_at))
    def set_sync_state(self, source,state): self.connection.execute("INSERT INTO sync_state VALUES (?,?) ON CONFLICT(source) DO UPDATE SET state_json=excluded.state_json",(source,json.dumps(state,sort_keys=True))); self.connection.commit()
    def get_sync_state(self, source):
        row=self.connection.execute("SELECT state_json FROM sync_state WHERE source=?",(source,)).fetchone(); return json.loads(row[0]) if row else {}
    def upsert_extraction_job(self, job_id,project_id,source,session_id,message_id,status,attempts,error_code,extraction_version,updated_at): self.connection.execute("INSERT INTO extraction_jobs VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,error_code=excluded.error_code,updated_at=excluded.updated_at",(job_id,project_id,source,session_id,message_id,status,attempts,error_code,extraction_version,updated_at)); self.connection.commit()
    def set_item_state(self,item_id,project_id,state): self.connection.execute("UPDATE knowledge_items SET state=? WHERE item_id=? AND project_id=?",(state,item_id,project_id)); self.connection.commit()
    def add_suppression(self,item_id,project_id,reason,timestamp): self.connection.execute("INSERT INTO suppression_log(item_id,project_id,reason,timestamp) VALUES (?,?,?,?)",(item_id,project_id,reason,timestamp)); self.connection.commit()
    def upsert_embedding_job(self,job_id,project_id,object_type,object_id,text,status,attempts,error_code,updated_at): self.connection.execute("INSERT INTO embedding_jobs VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET text=excluded.text,status=excluded.status,attempts=excluded.attempts,error_code=excluded.error_code,updated_at=excluded.updated_at",(job_id,project_id,object_type,object_id,text,status,attempts,error_code,updated_at)); self.connection.commit()
