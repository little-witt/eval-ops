import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aceval.codex_profiles import CodexProfileError, CodexProfileManager


FIXTURE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


class FakeResponse:
    def __init__(self, value):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.raw

    def __iter__(self):
        return iter(self.raw.splitlines(keepends=True))


class CodexProfileTests(unittest.TestCase):
    def _sources(self, root: Path, base_url="https://example.invalid/v1"):
        config = root / "config.toml"
        auth = root / "auth.json"
        config.write_text(
            'model = "gpt-5-test"\n'
            'model_provider = "OpenAI"\n'
            'notify = ["do-not-import"]\n'
            '[model_providers.OpenAI]\n'
            'name = "OpenAI"\n'
            'base_url = %s\n' % json.dumps(base_url) +
            'wire_api = "responses"\n'
            'requires_openai_auth = true\n'
            '[mcp_servers.untrusted]\n'
            'command = "do-not-import"\n',
            encoding="utf-8",
        )
        auth.write_text(json.dumps({"OPENAI_API_KEY": "test-secret-value"}), encoding="utf-8")
        return config, auth

    def test_separate_imports_are_private_isolated_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, auth = self._sources(root)
            before = {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in (config, auth)}
            manager = CodexProfileManager(root / "profiles", str(FIXTURE_CODEX))
            first = manager.import_file("default", "config", str(config))
            self.assertTrue(first["config_ready"])
            self.assertFalse(first["auth_ready"])
            second = manager.import_file("default", "auth", str(auth))
            self.assertTrue(second["ready"])
            imported_config = root / "profiles" / "default" / "config.toml"
            imported_auth = root / "profiles" / "default" / "auth.json"
            self.assertNotIn("notify", imported_config.read_text(encoding="utf-8"))
            self.assertNotIn("mcp_servers", imported_config.read_text(encoding="utf-8"))
            self.assertIn("model_providers.OpenAI", imported_config.read_text(encoding="utf-8"))
            self.assertEqual(0o600, stat.S_IMODE(imported_config.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(imported_auth.stat().st_mode))
            self.assertNotIn("test-secret-value", json.dumps(second))
            after = {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in (config, auth)}
            self.assertEqual(before, after)

    def test_model_catalog_and_real_turn_are_driven_by_app_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = []

            def fake_urlopen(request, timeout):
                del timeout
                if request.full_url.endswith("/models"):
                    return FakeResponse({"data": [{"id": "gpt-5-test"}]})
                requests.append(json.loads(request.data))
                return FakeResponse({"output_text": "FORGE_READY", "usage": {"input_tokens": 31, "output_tokens": 2, "total_tokens": 33}})

            with patch("aceval.codex_profiles.urlopen", side_effect=fake_urlopen):
                config, auth = self._sources(root)
                manager = CodexProfileManager(root / "profiles", str(FIXTURE_CODEX))
                manager.import_file("default", "config", str(config))
                manager.import_file("default", "auth", str(auth))
                profile = manager.refresh_models("default")
                self.assertEqual("responses-direct", profile["inference_mode"])
                self.assertEqual("gpt-5-test", profile["models"][0]["model"])
                self.assertEqual("medium", profile["models"][0]["default_reasoning_effort"])
                probe = manager.test_model("default", "gpt-5-test", "low")
                self.assertTrue(probe["ready"])
                self.assertEqual(33, probe["usage"]["total_tokens"])
                self.assertEqual("gpt-5-test", requests[0]["model"])

    def test_bridge_translates_aceval_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, auth = self._sources(root)
            auth.write_text(json.dumps({"tokens": {"access_token": "test-secret-value"}}), encoding="utf-8")
            manager = CodexProfileManager(root / "profiles", str(FIXTURE_CODEX))
            manager.import_file("default", "config", str(config))
            manager.import_file("default", "auth", str(auth))
            result = subprocess.run(
                (
                    sys.executable, "-m", "aceval.codex_bridge",
                    "--codex-executable", str(FIXTURE_CODEX),
                    "--codex-home", str(root / "profiles" / "default"),
                    "--model", "gpt-5-test", "--effort", "medium",
                ),
                input=json.dumps({"messages": [{"role": "user", "content": "probe"}], "tools": []}),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual("FORGE_READY", value["content"])
            self.assertEqual([], value["tool_calls"])

    def test_nvm_style_launcher_symlink_keeps_its_matching_runtime_on_minimal_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_directory = root / "package" / "bin"
            launcher_directory = root / "nvm" / "bin"
            target_directory.mkdir(parents=True)
            launcher_directory.mkdir(parents=True)
            target = target_directory / "codex.py"
            fixture = FIXTURE_CODEX.read_text(encoding="utf-8")
            target.write_text("#!/usr/bin/env forge-python\n" + fixture.split("\n", 1)[1], encoding="utf-8")
            target.chmod(0o755)
            runtime = launcher_directory / "forge-python"
            runtime.write_text("#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(sys.executable), encoding="utf-8")
            runtime.chmod(0o755)
            launcher = launcher_directory / "codex"
            launcher.symlink_to(target)
            config, auth = self._sources(root)
            auth.write_text(json.dumps({"tokens": {"access_token": "test-secret-value"}}), encoding="utf-8")

            with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
                manager = CodexProfileManager(root / "profiles", str(launcher))
                manager.import_file("default", "config", str(config))
                manager.import_file("default", "auth", str(auth))
                profile = manager.refresh_models("default")

            self.assertEqual(launcher.absolute(), manager.codex_executable)
            self.assertEqual("gpt-5-test", profile["models"][0]["id"])

    def test_cc_switch_managed_proxy_is_sanitized_and_uses_low_token_streaming(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            auth = root / "auth.json"
            config.write_text(
                'model = "gpt-5-test"\n'
                'model_provider = "custom"\n'
                '[model_providers.custom]\n'
                'name = "CC Switch"\n'
                'base_url = "http://127.0.0.1:15721/v1"\n'
                'wire_api = "responses"\n'
                'requires_openai_auth = true\n'
                'experimental_bearer_token = "PROXY_MANAGED"\n',
                encoding="utf-8",
            )
            auth.write_text(json.dumps({"OPENAI_API_KEY": "must-not-be-sent-to-the-local-proxy"}), encoding="utf-8")
            manager = CodexProfileManager(root / "profiles", str(FIXTURE_CODEX))
            requests = []

            def fake_urlopen(request, timeout):
                del timeout
                requests.append(request)
                if request.full_url.endswith("/models"):
                    return FakeResponse({"data": [{"id": "gpt-5-test"}]})
                return FakeResponse(
                    b'event: response.output_text.delta\n'
                    b'data: {"type":"response.output_text.delta","delta":"\\u200bFORGE_READY"}\n\n'
                    b'event: response.completed\n'
                    b'data: {"type":"response.completed","response":{"output":[],"usage":{"input_tokens":31,"output_tokens":2,"total_tokens":33}}}\n\n'
                    b'data: [DONE]\n\n'
                )

            with patch("aceval.codex_profiles.urlopen", side_effect=fake_urlopen):
                imported_profile = manager.import_cc_switch("default", str(config), str(auth))
                self.assertEqual("cc-switch", imported_profile["connection_mode"])
                self.assertEqual("cc-switch", imported_profile["import_source"])
                imported = (root / "profiles" / "default" / "config.toml").read_text(encoding="utf-8")
                self.assertIn('experimental_bearer_token = "PROXY_MANAGED"', imported)
                profile = manager.refresh_models("default")
                self.assertEqual("responses-streaming", profile["inference_mode"])
                probe = manager.test_model("default", "gpt-5-test", "low")
                self.assertTrue(probe["ready"])
                self.assertEqual(33, probe["usage"]["total_tokens"])
            post = next(request for request in requests if request.full_url.endswith("/responses"))
            self.assertEqual("Bearer PROXY_MANAGED", post.get_header("Authorization"))
            self.assertNotIn("must-not-be-sent", post.data.decode("utf-8"))

            config.write_text(config.read_text(encoding="utf-8").replace(
                'base_url = "http://127.0.0.1:15721/v1"',
                'base_url = "https://example.invalid/v1"',
            ).replace("PROXY_MANAGED", "real-secret-must-not-be-imported"), encoding="utf-8")
            manager.import_file("default", "config", str(config))
            imported = (root / "profiles" / "default" / "config.toml").read_text(encoding="utf-8")
            self.assertNotIn("real-secret-must-not-be-imported", imported)

    def test_invalid_auth_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "auth.json"
            invalid.write_text('{"not_a_credential":"value"}', encoding="utf-8")
            manager = CodexProfileManager(root / "profiles", str(FIXTURE_CODEX))
            with self.assertRaises(CodexProfileError):
                manager.import_file("default", "auth", str(invalid))
            if hasattr(os, "symlink"):
                link = root / "auth-link.json"
                link.symlink_to(invalid)
                with self.assertRaises(CodexProfileError):
                    manager.import_file("default", "auth", str(link))

    def test_non_codex_profile_directories_are_not_listed_as_codex_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "profiles" / "claude-default").mkdir(parents=True)
            (root / "profiles" / "claude-default" / "profile.json").write_text("{}", encoding="utf-8")
            manager = CodexProfileManager(root / "profiles", str(FIXTURE_CODEX))
            self.assertEqual(["default"], [profile["id"] for profile in manager.list_profiles()])


if __name__ == "__main__":
    unittest.main()
