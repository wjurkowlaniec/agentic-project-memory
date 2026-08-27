from __future__ import annotations

import json
import ipaddress
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class LMStudioError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


KNOWLEDGE_CANDIDATE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "statement", "state", "confidence", "evidence_message_id", "evidence_quote", "evidence_start", "evidence_end", "direct_user_statement", "conflicts_with", "supersedes"],
    "properties": {
        "kind": {"type": "string"},
        "statement": {"type": "string"},
        "state": {"type": "string", "enum": ["provisional", "confirmed", "superseded", "rejected", "suppressed"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_message_id": {"type": "string"},
        "evidence_quote": {"type": "string"},
        "evidence_start": {"type": "integer", "minimum": 0},
        "evidence_end": {"type": "integer", "minimum": 0},
        "direct_user_statement": {"type": "boolean"},
        "conflicts_with": {"type": "array", "items": {"type": "string"}},
        "supersedes": {"type": "array", "items": {"type": "string"}},
    },
}

EXTRACTION_RESPONSE_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "knowledge_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {"items": {"type": "array", "items": KNOWLEDGE_CANDIDATE_SCHEMA}},
        },
    },
}


class LMStudioClient:
    def __init__(self, base_url: str = "http://127.0.0.1:1234/v1", timeout: float = 60.0, *, allow_remote: bool = False, api_key: str | None = None):
        try:
            parsed = urlsplit(base_url)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError):
            raise LMStudioError("invalid_url") from None
        if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password or port is None and parsed.netloc.rsplit("@", 1)[-1].endswith(":"):
            raise LMStudioError("invalid_url")
        if not self._valid_host(hostname):
            raise LMStudioError("invalid_url")
        loopback = self.is_loopback_host(hostname)
        if not loopback and not allow_remote:
            raise LMStudioError("non_loopback_url")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.endpoint_scope = "local_loopback" if loopback else "remote_opt_in"
        self._api_key = api_key

    @staticmethod
    def _valid_host(hostname: str) -> bool:
        if any(char.isspace() for char in hostname) or len(hostname) > 253:
            return False
        try:
            ipaddress.ip_address(hostname)
            return True
        except ValueError:
            pass
        return bool(re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", hostname.rstrip(".")))

    @staticmethod
    def is_loopback_host(hostname: str) -> bool:
        return LMStudioClient._host_is_loopback(hostname)

    @staticmethod
    def _host_is_loopback(hostname: str) -> bool:
        if hostname.casefold() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    @staticmethod
    def is_loopback_url(base_url: str) -> bool:
        try:
            hostname = urlsplit(base_url).hostname
        except (TypeError, ValueError):
            return False
        return bool(hostname and LMStudioClient._host_is_loopback(hostname))

    def __repr__(self) -> str:
        return f"LMStudioClient(endpoint_scope={self.endpoint_scope!r})"

    def _request(self, method: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = "Bearer " + self._api_key
        request = Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise LMStudioError("http_error") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise LMStudioError("timeout" if isinstance(getattr(exc, "reason", exc), TimeoutError) else "connection_failed") from None
        except (ValueError, UnicodeError) as exc:
            raise LMStudioError("invalid_response") from None
        if not isinstance(decoded, dict):
            raise LMStudioError("invalid_response")
        return decoded

    def list_models(self) -> tuple[str, ...]:
        response = self._request("GET", "/models")
        data = response.get("data")
        if not isinstance(data, list):
            raise LMStudioError("invalid_response")
        ids = tuple(item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str))
        return ids

    def chat_json(self, model: str, messages: list[dict[str, str]], temperature: float = 0.0) -> dict[str, object]:
        response = self._request("POST", "/chat/completions", {"model": model, "messages": messages, "temperature": temperature, "response_format": EXTRACTION_RESPONSE_FORMAT})
        try:
            choices = response.get("choices")
            first = choices[0] if isinstance(choices, list) else None
            message = first.get("message") if isinstance(first, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            decoded = json.loads(content) if isinstance(content, str) else content
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LMStudioError("invalid_response") from None
        if not isinstance(decoded, dict):
            raise LMStudioError("invalid_response")
        return decoded

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        response = self._request("POST", "/embeddings", {"model": model, "input": texts})
        data = response.get("data")
        if not isinstance(data, list):
            raise LMStudioError("invalid_response")
        if len(data) != len(texts):
            raise LMStudioError("invalid_response")
        ordered: list[list[float] | None] = [None] * len(texts)
        seen: set[int] = set()
        try:
            for item in data:
                index = item["index"]
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(texts) or index in seen:
                    raise LMStudioError("invalid_response")
                embedding = item["embedding"]
                if not isinstance(embedding, list):
                    raise LMStudioError("invalid_response")
                ordered[index] = [float(value) for value in embedding]
                seen.add(index)
        except LMStudioError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise LMStudioError("invalid_response") from None
        if len(seen) != len(texts) or any(vector is None for vector in ordered):
            raise LMStudioError("invalid_response")
        return [vector for vector in ordered if vector is not None]
