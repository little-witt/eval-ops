import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from aceval.claude_profiles import ClaudeMessagesClient, ClaudeProfileError, ClaudeProfileManager


class FakeResponse:
    def __init__(self, value):
        self.value = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.value


class ClaudeProfileTests(unittest.TestCase):
    def test_active_cc_switch_claude_settings_are_imported_and_tested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text(json.dumps({
                "model": "claude-test",
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:15721",
                    "ANTHROPIC_AUTH_TOKEN": "test-secret",
                },
            }), encoding="utf-8")
            manager = ClaudeProfileManager(root / "profiles")
            profile = manager.import_cc_switch(str(settings))
            self.assertTrue(profile["ready"])
            self.assertEqual("claude", profile["provider"])
            self.assertEqual("claude-test", profile["models"][0]["model"])

            requests = []

            def fake_urlopen(request, timeout):
                requests.append((request, timeout))
                return FakeResponse({"content": [{"type": "text", "text": "FORGE_READY"}], "usage": {"input_tokens": 4, "output_tokens": 1}})

            with patch("aceval.claude_profiles.urlopen", side_effect=fake_urlopen):
                receipt = manager.test_model("claude-test")
            self.assertTrue(receipt["ready"])
            self.assertEqual(5, receipt["usage"]["total_tokens"])
            self.assertEqual("http://127.0.0.1:15721/v1/messages", requests[0][0].full_url)
            self.assertEqual("Bearer test-secret", requests[0][0].get_header("Authorization"))
            self.assertIsNone(requests[0][0].get_header("X-api-key"))

    def test_non_loopback_claude_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text(json.dumps({"model": "claude-test", "env": {"ANTHROPIC_BASE_URL": "https://example.invalid", "ANTHROPIC_AUTH_TOKEN": "secret"}}), encoding="utf-8")
            with self.assertRaises(ClaudeProfileError):
                ClaudeProfileManager(root / "profiles").import_cc_switch(str(settings))

    def test_refresh_discovers_cc_switch_models_and_effort_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text(json.dumps({"model": "sonnet[1m]", "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:15721", "ANTHROPIC_AUTH_TOKEN": "test-secret"}}), encoding="utf-8")
            manager = ClaudeProfileManager(root / "profiles")
            manager.import_cc_switch(str(settings))

            def fake_urlopen(request, timeout):
                del timeout
                if request.full_url.endswith("/v1/models"):
                    return FakeResponse({"models": []})
                body = {"error": {"message": "Unsupported. Available models: claude-haiku-4-5-20251001, claude-sonnet-5, claude-opus-5"}}
                raise HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(json.dumps(body).encode("utf-8")))

            with patch("aceval.claude_profiles.urlopen", side_effect=fake_urlopen):
                profile = manager.refresh_models()
            self.assertEqual(3, len(profile["models"]))
            sonnet = next(item for item in profile["models"] if item["model"] == "claude-sonnet-5")
            self.assertTrue(sonnet["is_default"])
            self.assertEqual(["low", "medium", "high", "xhigh", "max"], [item["id"] for item in sonnet["reasoning_efforts"]])
            haiku = next(item for item in profile["models"] if "haiku" in item["model"])
            self.assertEqual([], haiku["reasoning_efforts"])


if __name__ == "__main__":
    unittest.main()
