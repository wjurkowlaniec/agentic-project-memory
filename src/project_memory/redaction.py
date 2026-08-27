from __future__ import annotations

import re
from dataclasses import dataclass

REDACTION_VERSION = 1


@dataclass(frozen=True)
class RedactionMatch:
    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class RedactionResult:
    text: str
    matches: tuple[RedactionMatch, ...]
    version: int = REDACTION_VERSION


class Redactor:
    def __init__(self, canaries: tuple[str, ...] | list[str] = ()) -> None:
        self._canaries = tuple(canaries)
        self._patterns = (
            ("PRIVATE_KEY", re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.I | re.S), "[REDACTED:PRIVATE_KEY]"),
            ("AUTH_TOKEN", re.compile(r"(Authorization\s*:\s*(?:Bearer|Basic)\s+)[^\s,;]+", re.I), r"\1[REDACTED:AUTH_TOKEN]"),
            ("COOKIE", re.compile(r"(Cookie\s*:\s*)[^\r\n]+", re.I), r"\1[REDACTED:COOKIE]"),
            ("CREDENTIAL_URL", re.compile(r"(https?://)([^\s/@:]+):([^\s/@]+)@", re.I), r"\1[REDACTED:URL_USER]:[REDACTED:CREDENTIAL]@"),
            ("API_TOKEN", re.compile(r"(\b(?:x-api-key|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*)['\"]?[^\s,'\"]+", re.I), r"\1[REDACTED:API_TOKEN]"),
            ("SECRET_ASSIGNMENT", re.compile(r"((?<![-\w])(?!(?:x-api-key|api[_-]?key|access[_-]?token|auth[_-]?token)\b)[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|CREDENTIAL)[A-Z0-9_]*\s*=\s*)['\"]?[^\s,'\"]+", re.I), r"\1[REDACTED:SECRET]"),
        )

    def redact(self, text: str) -> RedactionResult:
        current = text
        origins: list[tuple[int, int]] = [(i, i + 1) for i in range(len(text))]
        matches: list[RedactionMatch] = []
        for kind, pattern, replacement in self._patterns:
            found = list(pattern.finditer(current))
            for match in found:
                span = origins[match.start():match.end()]
                if span:
                    matches.append(RedactionMatch(kind, span[0][0], span[-1][1]))
            pieces: list[str] = []
            new_origins: list[tuple[int, int]] = []
            cursor = 0
            for match in found:
                pieces.append(current[cursor:match.start()])
                new_origins.extend(origins[cursor:match.start()])
                expanded = match.expand(replacement)
                pieces.append(expanded)
                span = origins[match.start():match.end()]
                bounds = (span[0][0], span[-1][1]) if span else (match.start(), match.end())
                new_origins.extend([bounds] * len(expanded))
                cursor = match.end()
            pieces.append(current[cursor:])
            new_origins.extend(origins[cursor:])
            current, origins = "".join(pieces), new_origins
        for canary in self._canaries:
            if canary:
                pattern = re.compile(re.escape(canary))
                found = [
                    match for match in pattern.finditer(current)
                    if current.rfind("[REDACTED:", 0, match.start())
                    <= current.rfind("]", 0, match.start())
                ]
                for match in found:
                    span = origins[match.start():match.end()]
                    if span:
                        matches.append(RedactionMatch("CANARY", span[0][0], span[-1][1]))
                pieces: list[str] = []
                new_origins: list[tuple[int, int]] = []
                cursor = 0
                for match in found:
                    pieces.append(current[cursor:match.start()])
                    new_origins.extend(origins[cursor:match.start()])
                    span = origins[match.start():match.end()]
                    bounds = (span[0][0], span[-1][1]) if span else (match.start(), match.end())
                    replacement_text = "[REDACTED:CANARY]"
                    pieces.append(replacement_text)
                    new_origins.extend([bounds] * len(replacement_text))
                    cursor = match.end()
                pieces.append(current[cursor:])
                new_origins.extend(origins[cursor:])
                current, origins = "".join(pieces), new_origins
        return RedactionResult(current, tuple(matches))
