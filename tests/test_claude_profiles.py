import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

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

    def test_cc_switch_role_default_model_is_used_when_top_level_model_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text(json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:15721",
                    "ANTHROPIC_AUTH_TOKEN": "PROXY_MANAGED",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-8",
                },
            }), encoding="utf-8")
            profile = ClaudeProfileManager(root / "profiles").import_cc_switch(str(settings))
            self.assertEqual("claude-sonnet-4-6", profile["models"][0]["model"])

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

    def test_refresh_keeps_configured_model_when_proxy_catalog_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text(json.dumps({
                "model": "opus",
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:15721",
                    "ANTHROPIC_AUTH_TOKEN": "test-secret",
                },
            }), encoding="utf-8")
            manager = ClaudeProfileManager(root / "profiles")
            manager.import_cc_switch(str(settings))

            def fake_urlopen(request, timeout):
                del timeout
                if request.full_url.endswith("/v1/models"):
                    return FakeResponse({"models": []})
                return FakeResponse({"content": [{"type": "text", "text": "catalog"}]})

            with patch("aceval.claude_profiles.urlopen", side_effect=fake_urlopen):
                profile = manager.refresh_models()
            self.assertEqual(["opus"], [item["id"] for item in profile["models"]])
            self.assertTrue(profile["models"][0]["is_default"])

    def test_analysis_call_uses_bounded_output_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            profile.write_text(json.dumps({"base_url": "http://127.0.0.1:15721", "token": "secret"}), encoding="utf-8")
            requests = []

            def fake_urlopen(request, timeout):
                del timeout
                requests.append(json.loads(request.data.decode("utf-8")))
                return FakeResponse({"content": [{"type": "text", "text": "{}"}], "usage": {}})

            with patch("aceval.claude_profiles.urlopen", side_effect=fake_urlopen):
                ClaudeMessagesClient(profile).complete(({"role": "user", "content": "small"},), model="claude-test", max_output_tokens=99999)
            self.assertEqual(8192, requests[0]["max_tokens"])

    def test_complete_retries_transient_proxy_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            profile.write_text(json.dumps({"base_url": "http://127.0.0.1:15721", "token": "secret"}), encoding="utf-8")
            response = FakeResponse({"content": [{"type": "text", "text": "ok"}], "usage": {}})
            with patch("aceval.claude_profiles.urlopen", side_effect=[URLError("refused"), response]) as opened:
                result = ClaudeMessagesClient(profile).complete(({"role": "user", "content": "small"},), model="claude-test", max_output_tokens=10)
            self.assertEqual("ok", result["content"])
            self.assertEqual(2, opened.call_count)

    def test_complete_accepts_proxy_output_text_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            profile.write_text(json.dumps({"base_url": "http://127.0.0.1:15721", "token": "secret"}), encoding="utf-8")
            responses = [
                {"content": [{"type": "output_text", "output_text": "variant"}], "usage": {}},
                {"output_text": "top-level", "content": [], "usage": {}},
            ]
            for payload in responses:
                with self.subTest(payload=payload), patch("aceval.claude_profiles.urlopen", return_value=FakeResponse(payload)):
                    result = ClaudeMessagesClient(profile).complete(({"role": "user", "content": "small"},), model="claude-test", max_output_tokens=10)
                    self.assertIn(result["content"], ("variant", "top-level"))
            payload = {"content": [{"type": "tool_use", "name": "emit_json", "input": {"changes": []}}], "usage": {}}
            with patch("aceval.claude_profiles.urlopen", return_value=FakeResponse(payload)):
                result = ClaudeMessagesClient(profile).complete(({"role": "user", "content": "small"},), model="claude-test", max_output_tokens=10)
            self.assertEqual('{"changes": []}', result["content"])

    def test_complete_retries_transient_upstream_502(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            profile.write_text(json.dumps({"base_url": "http://127.0.0.1:15721", "token": "secret"}), encoding="utf-8")
            request = Request("http://127.0.0.1:15721/v1/messages")
            bad = HTTPError(request.full_url, 502, "Bad Gateway", {}, io.BytesIO(b'{"error":{"message":"upstream connect failed"}}'))
            with patch("aceval.claude_profiles.urlopen", side_effect=[bad, FakeResponse({"content": [{"type": "text", "text": "ok"}], "usage": {}})]) as opened:
                result = ClaudeMessagesClient(profile).complete(({"role": "user", "content": "small"},), model="claude-test", max_output_tokens=10)
            self.assertEqual("ok", result["content"])
            self.assertEqual(2, opened.call_count)

    def test_complete_retries_cloudflare_upstream_524(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            profile.write_text(json.dumps({"base_url": "http://127.0.0.1:15721", "token": "secret"}), encoding="utf-8")
            request = Request("http://127.0.0.1:15721/v1/messages")
            timeout = HTTPError(request.full_url, 524, "Origin Timeout", {}, io.BytesIO(b""))
            with patch("aceval.claude_profiles.urlopen", side_effect=[timeout, FakeResponse({"content": [{"type": "text", "text": "ok"}], "usage": {}})]) as opened:
                result = ClaudeMessagesClient(profile).complete(({"role": "user", "content": "small"},), model="claude-test", max_output_tokens=10)
            self.assertEqual("ok", result["content"])
            self.assertEqual(2, opened.call_count)

    def test_complete_reports_actionable_cloudflare_524_after_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            profile.write_text(json.dumps({"base_url": "http://127.0.0.1:15721", "token": "secret"}), encoding="utf-8")
            request = Request("http://127.0.0.1:15721/v1/messages")
            timeout_a = HTTPError(request.full_url, 524, "Origin Timeout", {}, io.BytesIO(b""))
            timeout_b = HTTPError(request.full_url, 524, "Origin Timeout", {}, io.BytesIO(b""))
            with patch("aceval.claude_profiles.urlopen", side_effect=[timeout_a, timeout_b]):
                with self.assertRaisesRegex(ClaudeProfileError, r"upstream timeout \(HTTP 524\)"):
                    ClaudeMessagesClient(profile).complete(({"role": "user", "content": "small"},), model="claude-test", max_output_tokens=10)


if __name__ == "__main__":
    unittest.main()
