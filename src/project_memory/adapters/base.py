from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import ProjectConfig
from ..models import NormalizedCommand, NormalizedMessage


@dataclass(frozen=True)
class SyncBatch:
    sessions_seen: int
    messages: tuple[NormalizedMessage, ...]
    next_state: dict[str, object]
    warnings: tuple[str, ...] = ()
    commands: tuple[NormalizedCommand, ...] = ()


class SourceAdapter(Protocol):
    def scan(self, config: ProjectConfig, state: dict[str, object]) -> SyncBatch:
        ...
