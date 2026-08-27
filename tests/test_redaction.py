import unittest

from project_memory.redaction import REDACTION_VERSION, Redactor


class RedactionTests(unittest.TestCase):
    def test_canaries_are_removed_and_matches_are_reported(self):
        samples = [
            "Authorization: Bearer CANARY-token",
            "Authorization: Basic CANARY-basic",
            "Cookie: session=CANARY-cookie",
            "DATABASE_PASSWORD=CANARY-password",
            "https://alice:CANARY-pass@example.test/path",
            "api_key = 'CANARY-api-key'",
            "access_token: CANARY-access-token",
            "-----BEGIN PRIVATE KEY-----\nCANARY-key\n-----END PRIVATE KEY-----",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                redacted = Redactor().redact(sample)
                self.assertNotIn("CANARY", redacted.text)
                self.assertGreater(len(redacted.matches), 0)

    def test_redaction_is_versioned_and_preserves_typed_markers(self):
        redactor = Redactor()
        result = redactor.redact(
            "Authorization: Bearer CANARY-token; x-api-key=CANARY-key"
        )
        self.assertEqual(REDACTION_VERSION, 1)
        self.assertEqual(result.version, REDACTION_VERSION)
        self.assertIn("[REDACTED:AUTH_TOKEN]", result.text)
        self.assertIn("[REDACTED:API_TOKEN]", result.text)
        self.assertNotIn("CANARY", result.text)

    def test_match_offsets_refer_to_original_input(self):
        source = "API_KEY=first TOKEN=second"
        result = Redactor().redact(source)
        self.assertEqual(
            [(source[m.start:m.end], m.kind) for m in result.matches],
            [("API_KEY=first", "API_TOKEN"), ("TOKEN=second", "SECRET_ASSIGNMENT")],
        )


if __name__ == "__main__":
    unittest.main()
