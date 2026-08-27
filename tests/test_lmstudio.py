import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

from project_memory.lmstudio import LMStudioClient, LMStudioError


class _FakeHandler(BaseHTTPRequestHandler):
    calls = []
    headers_seen = []

    def do_GET(self):
        self.__class__.headers_seen.append(dict(self.headers))
        self.__class__.calls.append((self.command, self.path, None))
        if self.path == "/v1/models":
            self._send({"data": [{"id": "local-a"}, {"id": "local-b"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        self.__class__.headers_seen.append(dict(self.headers))
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        self.__class__.calls.append((self.command, self.path, body))
        if self.path == "/v1/chat/completions":
            if body.get("response_format", {}).get("type") == "json_object":
                self._send({"error": "json_object is unsupported"}, 400)
                return
            if body.get("response_format", {}).get("type") != "json_schema":
                self._send({"error": "schema response required"}, 400)
                return
            self._send({"choices": [{"message": {"content": '{"items": []}'}}]})
        elif self.path == "/v1/embeddings":
            self._send({"data": [{"index": 1, "embedding": [3.0, 4.0]}, {"index": 0, "embedding": [1.0, 2.0]}]})
        else:
            self._send({"error": "not found"}, 404)

    def _send(self, payload, status=200):
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


class LMStudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _FakeHandler.calls = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def test_models_chat_json_and_embeddings_use_openai_compatible_endpoints(self):
        _FakeHandler.calls = []
        client = LMStudioClient(self.base, timeout=1)
        self.assertEqual(client.list_models(), ("local-a", "local-b"))
        self.assertEqual(client.chat_json("local-a", [{"role": "user", "content": "hello"}]), {"items": []})
        self.assertEqual(client.embed("embed", ["one", "two"]), [[1.0, 2.0], [3.0, 4.0]])
        self.assertEqual([call[0:2] for call in _FakeHandler.calls], [("GET", "/v1/models"), ("POST", "/v1/chat/completions"), ("POST", "/v1/embeddings")])
        self.assertNotIn("Authorization", _FakeHandler.headers_seen[0])

        chat_body = _FakeHandler.calls[1][2]
        self.assertEqual(chat_body["response_format"]["type"], "json_schema")
        self.assertEqual(chat_body["response_format"]["json_schema"]["name"], "knowledge_extraction")
        self.assertTrue(chat_body["response_format"]["json_schema"]["strict"])
        self.assertEqual(chat_body["response_format"]["json_schema"]["schema"], {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
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
                    },
                },
            },
        })

    def test_rejects_non_loopback_urls_without_sending_request(self):
        for url in ("http://192.0.2.1:1234/v1", "http://user:pass@127.0.0.1:1234/v1", "http:///missing-host/v1", "http://bad host/v1", "http://[not-an-ip]/v1"):
            with self.subTest(url=url):
                with self.assertRaises(LMStudioError):
                    LMStudioClient(url)

    def test_https_loopback_and_explicit_remote_opt_in_are_supported(self):
        local_client = LMStudioClient("https://127.0.0.1:1234/v1")
        self.assertEqual(local_client.base_url, "https://127.0.0.1:1234/v1")
        self.assertEqual(local_client.endpoint_scope, "local_loopback")
        with self.assertRaises(LMStudioError):
            LMStudioClient("https://example.test/v1")
        client = LMStudioClient("https://example.test/v1", allow_remote=True, api_key="never-persisted")
        self.assertEqual(client.endpoint_scope, "remote_opt_in")
        self.assertNotIn("never-persisted", repr(client))

    def test_remote_requests_send_only_bearer_auth_when_key_is_supplied(self):
        _FakeHandler.calls = []
        _FakeHandler.headers_seen = []
        client = LMStudioClient(self.base, api_key="test-key")
        client.list_models()
        self.assertEqual(_FakeHandler.headers_seen[-1]["Authorization"], "Bearer test-key")

    def test_http_errors_and_client_repr_do_not_leak_endpoint_or_secret(self):
        client = LMStudioClient("http://127.0.0.1:1/private", api_key="secret-key", timeout=0.01)
        with self.assertRaises(LMStudioError) as raised:
            client.list_models()
        text = repr(client) + str(raised.exception)
        for value in ("127.0.0.1", "private", "secret-key"):
            self.assertNotIn(value, text)

    def test_transport_failures_are_sanitized_and_timeout_is_bounded(self):
        client = LMStudioClient("http://127.0.0.1:1/v1", timeout=0.01)
        with self.assertRaises(LMStudioError) as raised:
            client.list_models()
        self.assertNotIn("hello", str(raised.exception))
        self.assertEqual(raised.exception.code, "connection_failed")
        self.assertIsNone(raised.exception.__cause__)

    def test_transport_and_response_parsing_errors_suppress_causes(self):
        client = LMStudioClient("http://127.0.0.1:1/v1", timeout=0.01)
        with self.assertRaises(LMStudioError) as transport:
            client.list_models()
        self.assertIsNone(transport.exception.__cause__)

        original = _FakeHandler.do_POST
        def bad_post(handler):
            length = int(handler.headers["Content-Length"])
            handler.rfile.read(length)
            handler._send({"choices": [{"message": {"content": "not-json"}}]})
        _FakeHandler.do_POST = bad_post
        try:
            with self.assertRaises(LMStudioError) as parsing:
                LMStudioClient(self.base).chat_json("model", [])
            self.assertIsNone(parsing.exception.__cause__)
        finally:
            _FakeHandler.do_POST = original

    def test_embeddings_reject_bad_indexes_and_count_mismatch(self):
        for data in (
            [{"index": 0, "embedding": [1]}, {"index": 0, "embedding": [2]}],
            [{"index": 2, "embedding": [1]}, {"index": 0, "embedding": [2]}],
            [{"index": True, "embedding": [1]}, {"index": 1, "embedding": [2]}],
            [{"index": 0, "embedding": [1]}],
        ):
            with self.subTest(data=data):
                original = _FakeHandler.do_POST
                def bad_post(handler):
                    length = int(handler.headers["Content-Length"])
                    body = json.loads(handler.rfile.read(length))
                    _FakeHandler.calls.append((handler.command, handler.path, body))
                    handler._send({"data": data})
                _FakeHandler.do_POST = bad_post
                try:
                    with self.assertRaises(LMStudioError) as raised:
                        LMStudioClient(self.base).embed("embed", ["one", "two"])
                    self.assertEqual(raised.exception.code, "invalid_response")
                finally:
                    _FakeHandler.do_POST = original


if __name__ == "__main__":
    unittest.main()
