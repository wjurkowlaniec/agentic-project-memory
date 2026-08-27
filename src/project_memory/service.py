from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ProjectConfig, ProjectPaths
from .command_history import ProjectCommandIndex
from .embeddings import EmbeddingIndex
from .extraction import ExtractionEngine
from .models import NormalizedMessage
from .redaction import REDACTION_VERSION, Redactor
from .retrieval import HybridRetriever
from .storage import MemoryRepository, VaultRepository, message_object_key, parse_message_object_key

EXTRACTION_VERSION = 1
_COMPETING_CHOICE = re.compile(r"^\s*(Use|Choose|Prefer|Set)\s+([A-Z][A-Za-z0-9_-]*)\s*$")

def _now() -> str: return datetime.now(timezone.utc).isoformat()

class ProjectMemoryService:
    def __init__(self, config: ProjectConfig, paths: ProjectPaths, adapters: dict[str, Any], *, redactor=None, extractor=None, embedder=None, embedding_model="nomic", embedding_dimension=768, extraction_enabled=False, clock=_now):
        self.config, self.paths, self.adapters = config, paths, dict(adapters)
        self.redactor, self.extractor, self.embedder = redactor or Redactor(), extractor, embedder
        self.extraction_enabled, self.clock = extraction_enabled, clock
        self.vault = VaultRepository(paths.vault_db); self.memory = MemoryRepository(paths.memory_db)
        self.commands = ProjectCommandIndex(paths.project_dir / "commands.sqlite3", redactor=self.redactor)
        self.embedding_index = EmbeddingIndex(self.memory, model=embedding_model, dimension=embedding_dimension)
        self.retriever = HybridRetriever(self.memory, self.embedding_index, embedder, model=embedding_model, redactor=self.redactor)
        self.receipt_db_insert_hook = None
        self.receipt_after_rename_hook = None
        self.receipt_before_commit_hook = None
        self.rebuild_failure_injector = None

    @classmethod
    def init(cls, config, paths, adapters=None, **kwargs): return cls(config, paths, adapters or {}, **kwargs)
    def close(self): self.vault.close(); self.memory.close(); self.commands.close()

    def _redact_value(self, value):
        if isinstance(value, str): return self.redactor.redact(value).text
        if isinstance(value, dict): return {str(k): self._redact_value(v) for k, v in value.items()}
        if isinstance(value, list): return [self._redact_value(v) for v in value]
        if isinstance(value, tuple): return [self._redact_value(v) for v in value]
        return value

    def _redacted_message(self, message):
        red = self.redactor.redact(message.content)
        metadata = {k: self._redact_value(v) for k, v in message.metadata.items()}
        source_path = self.redactor.redact(message.source_path).text if isinstance(message.source_path, str) else message.source_path
        metadata["redaction_version"] = REDACTION_VERSION
        return NormalizedMessage(message.source,message.session_id,message.message_id,self.config.project_id,message.role,message.timestamp,red.text,source_path,message.source_hash,metadata)

    def _embed(self, object_type, object_id, text):
        if self.embedder is None: return
        job_id = hashlib.sha256(f"{self.config.project_id}:{object_type}:{object_id}".encode()).hexdigest()
        row = self.memory.connection.execute("SELECT attempts FROM embedding_jobs WHERE job_id=?", (job_id,)).fetchone()
        attempts = int(row[0]) + 1 if row else 1
        try:
            vector = self.embedder.embed(self.embedding_index.model, [text])[0]
            self.embedding_index.upsert(object_type, object_id, self.config.project_id, text, vector)
            self.memory.upsert_embedding_job(job_id,self.config.project_id,object_type,object_id,text,"succeeded",attempts,None,self.clock())
        except Exception:
            self.memory.upsert_embedding_job(job_id,self.config.project_id,object_type,object_id,text,"pending",attempts,"embedding_unavailable",self.clock())

    def _retry_embeddings(self):
        if self.embedder is None: return
        rows = self.memory.connection.execute("SELECT * FROM embedding_jobs WHERE project_id=? AND status='pending'", (self.config.project_id,)).fetchall()
        for row in rows: self._embed(row["object_type"],row["object_id"],row["text"])

    def _store_candidates(self, message, job):
        ids = []
        for validated in getattr(job, "candidates", ()):
            c = validated.candidate
            item_id = hashlib.sha256(f"{self.config.project_id}:{message.source}:{message.session_id}:{message.message_id}:{c.statement}".encode()).hexdigest()[:24]
            self.memory.add_knowledge_candidate(self.config.project_id,item_id,c.kind,c.statement,c.state,c.confidence,message.source,message.session_id,message.message_id,c.evidence_quote,c.evidence_start,c.evidence_end,c.conflicts_with,c.supersedes,EXTRACTION_VERSION,self.clock())
            self._embed("knowledge", item_id, c.statement); ids.append(item_id)
        return set(ids)

    def _extract(self, message, *, job_id=None):
        job = self.extractor.extract_turn(message)
        jid = job_id or hashlib.sha256(f"{self.config.project_id}:{message.source}:{message.session_id}:{message.message_id}".encode()).hexdigest()
        row = self.memory.connection.execute("SELECT attempts FROM extraction_jobs WHERE job_id=?", (jid,)).fetchone()
        attempts = int(row[0]) + 1 if row else 1
        self.memory.upsert_extraction_job(jid,self.config.project_id,message.source,message.session_id,message.message_id,job.status,attempts,job.error_code,EXTRACTION_VERSION,self.clock())
        if job.status == "succeeded":
            keep = self._store_candidates(message, job)
            self.memory.connection.execute("UPDATE knowledge_items SET state='superseded' WHERE project_id=? AND evidence_source=? AND evidence_session_id=? AND evidence_message_id=? AND state NOT IN ('superseded','suppressed','rejected') AND item_id NOT IN ({})".format(",".join("?"*len(keep)) if keep else "''"), (self.config.project_id,message.source,message.session_id,message.message_id,*keep)); self.memory.connection.commit()
        return job

    def sync(self, *, extract=None):
        summary={"sessions":0,"messages":0,"commands":0,"pending":0,"warnings":[]}; do_extract=self.extraction_enabled if extract is None else extract
        self._retry_embeddings()
        if do_extract and self.extractor is not None:
            for row in self.memory.connection.execute("SELECT * FROM extraction_jobs WHERE project_id=? AND status='pending'",(self.config.project_id,)).fetchall():
                m=self.memory.connection.execute("SELECT * FROM messages WHERE project_id=? AND source=? AND session_id=? AND message_id=?",(self.config.project_id,row["source"],row["session_id"],row["message_id"])).fetchone()
                if m:
                    normalized=NormalizedMessage(m["source"],m["session_id"],m["message_id"],m["project_id"],m["role"],m["timestamp"],m["content"],m["source_path"],m["source_hash"],json.loads(m["metadata_json"]))
                    result=self._extract(normalized,job_id=row["job_id"])
                    if result.status=="pending": summary["pending"]+=1
        for source,adapter in self.adapters.items():
            state=self.memory.get_sync_state(source)
            try: batch=adapter.scan(self.config,state)
            except Exception: summary["warnings"].append(f"{source}: adapter_failed"); continue
            for message in batch.messages:
                if message.project_id != self.config.project_id: continue
                self.vault.upsert_message(message); derived=self._redacted_message(message); self.memory.upsert_message(derived); self._embed("message",message_object_key(message.source,message.session_id,message.message_id),derived.content); summary["messages"]+=1
                if do_extract and self.extractor is not None:
                    result=self._extract(derived)
                    if result.status=="pending": summary["pending"]+=1
            command_result = self.commands.upsert_commands(batch.commands, self.config.project_id)
            summary["commands"] += command_result["events"]
            self.memory.set_sync_state(source,batch.next_state); summary["sessions"]+=batch.sessions_seen; summary["warnings"].extend(batch.warnings)
        return summary

    def list_commands(self, limit=50):
        return self.commands.list_commands(self.config.project_id, limit)

    def search(self, query, limit=10, *, include_history=False): return self.retriever.search(self.config.project_id,query,limit,include_history=include_history)

    def _source_hash(self,result):
        if result.object_type=="message":
            row=self.memory.connection.execute("SELECT source_hash FROM messages WHERE object_key=? AND project_id=?",(result.object_id,self.config.project_id)).fetchone()
        else:
            row=self.memory.connection.execute("SELECT m.source_hash FROM messages m JOIN knowledge_items k ON k.project_id=m.project_id AND k.evidence_source=m.source AND k.evidence_session_id=m.session_id AND k.evidence_message_id=m.message_id WHERE k.item_id=? AND k.project_id=? AND m.project_id=?",(result.object_id,self.config.project_id,self.config.project_id)).fetchone()
        return row[0] if row else ""

    def preflight(self, query, *, limit=10):
        self.sync()
        redacted_query=self.redactor.redact(query).text
        results=self.search(redacted_query,limit)
        message_ids={(r.source,r.session_id,r.object_id) for r in results if r.object_type=="message"}
        for row in self.memory.connection.execute("SELECT * FROM knowledge_items WHERE project_id=? AND state NOT IN ('suppressed','rejected')",(self.config.project_id,)).fetchall():
            if (row["evidence_source"],row["evidence_session_id"],message_object_key(row["evidence_source"],row["evidence_session_id"],row["evidence_message_id"])) in message_ids and not any(r.object_id==row["item_id"] for r in results):
                results.append(SimpleNamespace(object_id=row["item_id"],object_type="knowledge",quote=row["evidence_quote"],source=row["evidence_source"],session_id=row["evidence_session_id"],timestamp=row["timestamp"],state=row["state"],kind=row["kind"],inspect_command=f"pmem inspect --root <project> {row['item_id']}"))
        knowledge=[r for r in results if r.object_type=="knowledge" and r.state not in {"suppressed","superseded","rejected"}]
        links={tuple(r) for r in self.memory.connection.execute("SELECT source_id,target_id FROM item_links WHERE project_id=? AND relation='conflicts_with'",(self.config.project_id,)).fetchall()}
        conflict_ids=set()
        conflict_reasons=[]
        for a in knowledge:
            for b in knowledge:
                if a.object_id==b.object_id or a.kind!=b.kind: continue
                pair=tuple(sorted((a.object_id,b.object_id)))
                explicit=(a.object_id,b.object_id) in links or (b.object_id,a.object_id) in links
                ma=_COMPETING_CHOICE.match(a.quote); mb=_COMPETING_CHOICE.match(b.quote)
                competing=ma is not None and mb is not None
                if ma is not None and mb is not None:
                    competing=ma.group(1).lower()==mb.group(1).lower() and ma.group(2).lower()!=mb.group(2).lower() and a.state==b.state=="confirmed"
                if explicit or competing:
                    conflict_ids.update(pair); conflict_reasons.append({"object_ids":list(pair),"reason":"explicit_conflicts_with" if explicit else "deterministic_competing_choice"})
        conflict_reasons=list({(tuple(r["object_ids"]),r["reason"]):r for r in conflict_reasons}.values())
        citations=[{"object_id":r.object_id,"object_type":r.object_type,"quote":r.quote,"source":r.source,"session_id":r.session_id,"timestamp":r.timestamp,"state":r.state,"inspect_command":r.inspect_command} for r in results]
        payload={"project_id":self.config.project_id,"query_hash":hashlib.sha256(redacted_query.encode()).hexdigest(),"timestamp":self.clock(),"redaction_version":REDACTION_VERSION,"extraction_version":EXTRACTION_VERSION,"result_object_ids":[r.object_id for r in results],"source_hashes":sorted({self._source_hash(r) for r in results}),"conflict_ids":sorted(conflict_ids),"conflict_reasons":conflict_reasons,"material_conflict":bool(conflict_ids),"citations":citations}
        rid=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(); payload["receipt_id"]=rid; path=self.paths.receipts_dir/f"{rid}.json"; self.paths.receipts_dir.mkdir(parents=True,exist_ok=True)
        fd,temp=tempfile.mkstemp(prefix=".receipt-",dir=self.paths.receipts_dir); os.fchmod(fd,0o600)
        try:
            with os.fdopen(fd,"w",encoding="utf-8") as f: json.dump(payload,f,sort_keys=True,indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
            self.memory.connection.commit()
            self.memory.connection.execute("BEGIN")
            if self.receipt_db_insert_hook: self.receipt_db_insert_hook()
            self.memory.connection.execute("INSERT INTO preflight_receipts VALUES (?,?,?,?,?)",(rid,self.config.project_id,payload["query_hash"],payload["timestamp"],json.dumps(payload,sort_keys=True)))
            os.replace(temp,path); path.chmod(0o600)
            if self.receipt_after_rename_hook: self.receipt_after_rename_hook()
            if self.receipt_before_commit_hook: self.receipt_before_commit_hook()
            self.memory.connection.commit()
        except Exception:
            self.memory.connection.rollback(); Path(temp).unlink(missing_ok=True); path.unlink(missing_ok=True); raise
        payload["receipt_path"]=str(path); return payload

    def inspect(self, object_id, *, raw=False):
        if object_id.startswith("message:"):
            source,session,mid=parse_message_object_key(object_id)
            if raw: row=self.vault.latest(self.config.project_id,source,session,mid)
            else: row=self.memory.connection.execute("SELECT * FROM messages WHERE object_key=? AND project_id=?",(object_id,self.config.project_id)).fetchone()
            return dict(row) if row else None
        row=self.memory.connection.execute("SELECT * FROM knowledge_items WHERE item_id=? AND project_id=?",(object_id,self.config.project_id)).fetchone(); return dict(row) if row else None

    def suppress(self,item_id,reason):
        if not isinstance(reason,str) or not reason.strip(): raise ValueError("suppression_reason_required")
        row=self.memory.connection.execute("SELECT 1 FROM knowledge_items WHERE item_id=? AND project_id=?",(item_id,self.config.project_id)).fetchone()
        if not row: raise ValueError("unknown_item")
        redacted_reason=self.redactor.redact(reason.strip()).text
        self.memory.set_item_state(item_id,self.config.project_id,"suppressed"); self.memory.add_suppression(item_id,self.config.project_id,redacted_reason,self.clock())

    def correct(self,item_id,statement,*,confirmed=False):
        if not confirmed or not isinstance(statement,str) or not statement.strip(): raise ValueError("user_confirmation_required")
        old=self.inspect(item_id)
        if old is None: raise ValueError("unknown_item")
        if "kind" not in old or "evidence_message_id" not in old: raise ValueError("knowledge_item_required")
        session_id=hashlib.sha256(f"{self.config.project_id}:correction-session:{item_id}:{statement}".encode()).hexdigest()[:24]; message_id=hashlib.sha256(f"{session_id}:message".encode()).hexdigest()[:24]
        raw=NormalizedMessage("pmem",session_id,message_id,self.config.project_id,"user",self.clock(),statement,"pmem",hashlib.sha256(statement.encode()).hexdigest(),{"correction_for":item_id})
        self.vault.upsert_message(raw); derived=self._redacted_message(raw); self.memory.upsert_message(derived)
        new_id=hashlib.sha256(f"{self.config.project_id}:correction:{item_id}:{statement}".encode()).hexdigest()[:24]
        redacted=derived.content
        self.memory.add_knowledge_candidate(self.config.project_id,new_id,old["kind"],redacted,"confirmed",1.0,"pmem",session_id,message_id,redacted,0,len(redacted),(),(),EXTRACTION_VERSION,self.clock())
        self.memory.add_item_link(new_id,item_id,"supersedes",self.config.project_id,self.clock()); self.memory.set_item_state(item_id,self.config.project_id,"superseded")
        self._embed("knowledge",new_id,redacted)
        return {"item_id":new_id,"state":"confirmed","session_id":session_id,"message_id":message_id}

    def rebuild(self):
        self.memory.connection.execute("PRAGMA wal_checkpoint(FULL)"); self.memory.close()
        backup=self.paths.memory_db.with_name(f"memory.sqlite3.bak-{self.clock().replace(':','').replace('+','-')}"); shutil.copy2(self.paths.memory_db,backup); backup.chmod(0o600)
        temp=self.paths.memory_db.with_name(".memory-rebuild.sqlite3"); Path(temp).unlink(missing_ok=True); shutil.copy2(self.paths.memory_db,temp)
        rebuilt=MemoryRepository(temp)
        try:
            if self.rebuild_failure_injector: self.rebuild_failure_injector()
            for row in self.vault.connection.execute("SELECT source,session_id,message_id,project_id,role,timestamp,content,source_path,source_hash,metadata_json FROM messages WHERE project_id=? AND id IN (SELECT MAX(id) FROM messages WHERE project_id=? GROUP BY source,session_id,message_id)",(self.config.project_id,self.config.project_id)).fetchall():
                if self.rebuild_failure_injector: self.rebuild_failure_injector()
                red=self._redacted_message(NormalizedMessage(row["source"],row["session_id"],row["message_id"],row["project_id"],row["role"],row["timestamp"],row["content"],row["source_path"],row["source_hash"],json.loads(row["metadata_json"])))
                check_content=re.sub(r"\[REDACTED:[A-Z_]+\]", "", red.content)
                if any(c and c in check_content for c in getattr(self.redactor,"_canaries",())): raise ValueError("redaction_canary_failed")
                rebuilt.upsert_message(red)
            if rebuilt.connection.execute("PRAGMA foreign_key_check").fetchall(): raise ValueError("foreign_key_check_failed")
        except Exception:
            rebuilt.close(); Path(temp).unlink(missing_ok=True); self.memory=MemoryRepository(self.paths.memory_db); self.embedding_index=EmbeddingIndex(self.memory,model=self.embedding_index.model,dimension=self.embedding_index.dimension); self.retriever=HybridRetriever(self.memory,self.embedding_index,self.embedder,model=self.embedding_index.model,redactor=self.redactor); raise
        rebuilt.close(); os.replace(temp,self.paths.memory_db); self.memory=MemoryRepository(self.paths.memory_db); self.embedding_index=EmbeddingIndex(self.memory,model=self.embedding_index.model,dimension=self.embedding_index.dimension); self.retriever=HybridRetriever(self.memory,self.embedding_index,self.embedder,model=self.embedding_index.model,redactor=self.redactor); return str(backup)

    def status(self): return {"project_id":self.config.project_id,"messages":self.memory.count_messages(),"pending_extraction":self.memory.connection.execute("SELECT COUNT(*) FROM extraction_jobs WHERE status='pending' AND project_id=?",(self.config.project_id,)).fetchone()[0],"pending_embedding":self.memory.connection.execute("SELECT COUNT(*) FROM embedding_jobs WHERE status='pending' AND project_id=?",(self.config.project_id,)).fetchone()[0]}
