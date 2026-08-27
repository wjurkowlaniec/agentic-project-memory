from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from typing import Callable, Any

from ..config import ProjectConfig
from ..models import NormalizedCommand, NormalizedMessage
from ..redaction import Redactor
from .base import SyncBatch


class HermesAdapter:
    source = "hermes"

    def __init__(self, *, command_runner: Callable[..., Any] = subprocess.run,
                 streaming_runner: Callable[..., Any] | None = None,
                 redactor: Redactor | None = None, timeout: float = 120.0) -> None:
        self.command_runner = command_runner
        self.streaming_runner = streaming_runner or (command_runner if command_runner is not subprocess.run else subprocess.Popen)
        # Redaction belongs to ProjectMemoryService, after the exact message is
        # written to the private vault.  Keeping a redactor here caused a
        # destructive double-redaction boundary.
        self.redactor = redactor or Redactor()
        self.timeout = timeout

    def archive_unredacted(self, config: ProjectConfig, writer: Callable[[str], None]) -> bool:
        """Send the raw exporter stream to a caller-owned restricted vault writer."""
        command = ["hermes", "sessions", "export", "--format", "jsonl", "--cwd", str(config.root), "--yes", "-"]
        process = self.streaming_runner(command, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
        stdout = getattr(process, "stdout", None)
        if isinstance(stdout, str):
            if process.returncode == 0:
                writer(stdout)
            return process.returncode == 0
        timed_out = threading.Event()

        def stop_exporter() -> None:
            timed_out.set()
            try:
                process.kill()
            except ProcessLookupError:
                pass

        def terminate_exporter() -> None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            finally:
                process.wait()

        watchdog = threading.Timer(self.timeout, stop_exporter)
        watchdog.daemon = True
        watchdog.start()
        try:
            for chunk in process.stdout:
                writer(chunk)
            return not timed_out.is_set() and process.wait() == 0
        except BaseException:
            terminate_exporter()
            raise
        finally:
            watchdog.cancel()
            stdout = getattr(process, "stdout", None)
            close = getattr(stdout, "close", None)
            if callable(close):
                close()
            if timed_out.is_set():
                process.wait()

    def scan(self, config: ProjectConfig, state: dict[str, object]) -> SyncBatch:
        command = ["hermes", "sessions", "export", "--format", "jsonl", "--cwd", str(config.root), "--yes", "-"]
        newer = state.get("newer_than")
        command_backfill = state.get("command_index_version") != 1
        if newer and not command_backfill:
            command[7:7] = ["--newer-than", str(newer)]
        messages_by_id: dict[tuple[str, str], NormalizedMessage] = {}
        session_ids: set[str] = set()
        commands_by_id: dict[tuple[str, str], NormalizedCommand] = {}
        seen_hashes = {str(key): str(value) for key, value in dict(state.get("message_hashes", {})).items()}
        seen_command_hashes = {str(key): str(value) for key, value in dict(state.get("command_hashes", {})).items()}
        def parse_line(line: str) -> None:
            nonlocal newer
            try:
                session = json.loads(line)
            except (ValueError, TypeError):
                return
            cwd = session.get("cwd", session.get("project_root"))
            if cwd is None or Path(str(cwd)).expanduser().resolve() not in {config.root, *config.aliases}:
                return
            session_id = str(session.get("session_id", session.get("id", "")))
            updated = str(session.get("updated_at", session.get("updated", "")))
            for item in session.get("messages", []):
                role = item.get("role")
                message_id = str(item.get("id", ""))
                timestamp = str(item.get("timestamp", updated))
                if role == "assistant":
                    for index, call in enumerate(item.get("tool_calls", [])):
                        if not isinstance(call, dict):
                            continue
                        function = call.get("function", call)
                        if not isinstance(function, dict) or function.get("name") != "terminal":
                            continue
                        arguments = function.get("arguments", function.get("input", {}))
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except ValueError:
                                continue
                        if not isinstance(arguments, dict) or not isinstance(arguments.get("command"), str):
                            continue
                        workdir = Path(str(arguments.get("workdir") or cwd)).expanduser().resolve()
                        members = {config.root, *config.aliases}
                        if not any(workdir == member or member in workdir.parents for member in members):
                            continue
                        command_text = arguments["command"].strip()
                        command_id = str(call.get("id") or call.get("call_id") or f"{message_id}:{index}")
                        if not command_text or not command_id:
                            continue
                        digest = hashlib.sha256(f"{command_text}\0{workdir}".encode("utf-8")).hexdigest()
                        checkpoint = f"{session_id}:{command_id}"
                        if seen_command_hashes.get(checkpoint) == digest:
                            continue
                        commands_by_id[(session_id, command_id)] = NormalizedCommand(
                            self.source, session_id, command_id, config.project_id,
                            timestamp, command_text, str(workdir), digest,
                        )
                        seen_command_hashes[checkpoint] = digest
                        session_ids.add(session_id)
                if role not in {"user", "assistant"} or not message_id:
                    continue
                content = item.get("content", item.get("text", ""))
                if not isinstance(content, str) or not content:
                    continue
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                checkpoint_key = f"{session_id}:{message_id}"
                if seen_hashes.get(checkpoint_key) == digest:
                    continue
                messages_by_id[(session_id, message_id)] = NormalizedMessage(self.source, session_id, message_id, config.project_id,
                    role, timestamp, content, None, digest, {})
                seen_hashes[checkpoint_key] = digest
                session_ids.add(session_id)
                if timestamp and (not newer or timestamp > str(newer)):
                    newer = timestamp
        warnings: list[str] = []
        runner = self.command_runner if self.command_runner is not subprocess.run else self.streaming_runner
        # Keep the old injected CompletedProcess contract for unit tests and
        # callers that provide a bounded, already-completed runner.
        if runner is self.command_runner and self.command_runner is not subprocess.run:
            try:
                completed = self.command_runner(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                                text=True, timeout=self.timeout)
            except TypeError as exc:
                # Popen-compatible injected runners do not accept run()'s
                # timeout keyword; the watchdog below owns that deadline.
                if "timeout" not in str(exc):
                    raise
                completed = self.command_runner(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                                text=True)
            if not isinstance(getattr(completed, "stdout", None), str):
                process = completed
                timed_out = threading.Event()
                def kill_exporter() -> None:
                    timed_out.set()
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                watchdog = threading.Timer(self.timeout, kill_exporter)
                watchdog.daemon = True
                watchdog.start()
                try:
                    for line in process.stdout:
                        parse_line(line)
                    returncode = process.wait()
                finally:
                    watchdog.cancel()
                    if timed_out.is_set():
                        process.wait()
                    close = getattr(process.stdout, "close", None)
                    if callable(close):
                        close()
                if timed_out.is_set():
                    warnings.append("Hermes exporter timed out")
                elif returncode != 0:
                    warnings.append(f"Hermes exporter exit {returncode}")
                completed = None
            if completed is None:
                pass
            elif completed.returncode != 0:
                return SyncBatch(0, (), dict(state), (f"Hermes exporter exit {completed.returncode}",), ())
            elif isinstance(completed.stdout, str):
                for line in completed.stdout.splitlines():
                    parse_line(line)
        else:
            process = runner(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True)
            timed_out = threading.Event()
            def kill_exporter() -> None:
                timed_out.set()
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            watchdog = threading.Timer(self.timeout, kill_exporter)
            watchdog.daemon = True
            watchdog.start()
            try:
                for line in process.stdout:
                    parse_line(line)
                returncode = process.wait()
            finally:
                watchdog.cancel()
                if timed_out.is_set():
                    process.wait()
                close = getattr(process.stdout, "close", None)
                if callable(close):
                    close()
            if timed_out.is_set():
                warnings.append("Hermes exporter timed out")
            elif returncode != 0:
                warnings.append(f"Hermes exporter exit {returncode}")
        if warnings:
            return SyncBatch(0, (), dict(state), tuple(warnings), ())
        next_state = dict(state)
        if newer:
            next_state["newer_than"] = newer
        next_state["message_hashes"] = seen_hashes
        next_state["command_hashes"] = seen_command_hashes
        next_state["command_index_version"] = 1
        return SyncBatch(len(session_ids), tuple(messages_by_id.values()), next_state, tuple(warnings), tuple(commands_by_id.values()))
