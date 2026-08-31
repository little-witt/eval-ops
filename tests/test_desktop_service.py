import tempfile
import unittest
from pathlib import Path
import json
import threading
import time

from aceval.desktop_service import DESKTOP_SERVICE_API_VERSION, DesktopService


FIXTURE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


class DesktopServiceTests(unittest.TestCase):
    def test_retry_failed_serializes_rapid_requests_before_batch_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DesktopService(Path(directory) / "tasks")
            entered = threading.Event()
            release = threading.Event()

            def fake_retry(task_id, *, purpose):
                entered.set()
                release.wait(timeout=2)
                return {"cases": []}

            service.kernel.retry_failed = fake_retry
            service._start_operation = lambda *args, **kwargs: {"status": "running"}
            results = []

            def invoke():
                try:
                    results.append(("ok", service.handle("tasks.retry_failed", {"task_id": "task-1", "purpose": "evaluation"})))
                except Exception as exc:
                    results.append(("error", str(exc)))

            first = threading.Thread(target=invoke)
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second = threading.Thread(target=invoke)
            second.start()
            second.join(timeout=1)
            release.set()
            first.join(timeout=2)

            self.assertEqual(2, len(results))
            self.assertEqual(1, sum(item[0] == "ok" for item in results))
            self.assertTrue(any(item[0] == "error" and "正在重试" in item[1] for item in results))
            service.executor.shutdown(wait=True)

    def test_bootstrap_reports_real_local_capabilities_without_opening_a_port(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DesktopService(Path(directory) / "tasks")
            value = service.handle("system.bootstrap", {})
            self.assertEqual(DESKTOP_SERVICE_API_VERSION, value["api_version"])
            self.assertTrue(value["capabilities"]["skill_harness"])
            self.assertTrue(value["capabilities"]["d2c_browser_worker"])
            self.assertTrue(value["environment_health"]["kernel"]["ready"])
            self.assertIn("git", value["environment_health"])
            self.assertIn("codex", value["environment_health"])
            self.assertTrue(value["capabilities"]["codex_local_brain"])
            self.assertEqual([], service.handle("tasks.list", {})["tasks"])
            service.executor.shutdown(wait=True)

    def test_codex_profile_files_are_imported_separately_and_models_are_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            auth = root / "auth.json"
            config.write_text('model = "gpt-5-test"\n', encoding="utf-8")
            auth.write_text(json.dumps({"tokens": {"access_token": "test-only"}}), encoding="utf-8")
            service = DesktopService(
                root / "tasks",
                model_profile_root=root / "models",
                codex_executable=str(FIXTURE_CODEX),
            )
            service.handle("models.import_codex_file", {"profile_id": "default", "kind": "config", "source_path": str(config)})
            service.handle("models.import_codex_file", {"profile_id": "default", "kind": "auth", "source_path": str(auth)})
            profile = service.handle("models.refresh_codex", {"profile_id": "default"})
            self.assertTrue(profile["ready"])
            self.assertEqual("gpt-5-test", profile["models"][0]["id"])
            probe = service.handle("models.test_codex", {"profile_id": "default", "model_id": "gpt-5-test", "reasoning_effort": "medium"})
            self.assertTrue(probe["ready"])
            resolved = service._local_analysis({"provider": "codex", "profile_id": "default", "model_id": "gpt-5-test", "reasoning_effort": "low"})
            self.assertEqual("codex:gpt-5-test", resolved["model_id"])
            self.assertNotIn("OPENAI_API_KEY", resolved["env_allowlist"])
            self.assertIn("aceval.codex_bridge", resolved["model_command"])
            service.executor.shutdown(wait=True)

    def test_unknown_renderer_method_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DesktopService(Path(directory) / "tasks")
            with self.assertRaises(ValueError):
                service.handle("shell.execute", {"command": "whoami"})
            service.executor.shutdown(wait=True)

    def test_cc_switch_pair_import_is_exposed_as_a_bounded_model_rpc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            auth = root / "auth.json"
            config.write_text(
                'model = "gpt-5-test"\nmodel_provider = "custom"\n'
                '[model_providers.custom]\nbase_url = "http://127.0.0.1:15721/v1"\n'
                'wire_api = "responses"\nexperimental_bearer_token = "PROXY_MANAGED"\n',
                encoding="utf-8",
            )
            auth.write_text(json.dumps({"OPENAI_API_KEY": "test-only"}), encoding="utf-8")
            service = DesktopService(
                root / "tasks",
                model_profile_root=root / "models",
                codex_executable=str(FIXTURE_CODEX),
            )
            profile = service.handle("models.import_codex_cc_switch", {
                "profile_id": "default", "config_path": str(config), "auth_path": str(auth),
            })
            self.assertEqual("cc-switch", profile["connection_mode"])
            self.assertEqual("cc-switch", profile["import_source"])
            service.executor.shutdown(wait=True)

    def test_cc_switch_auto_import_falls_back_to_active_claude_code_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = root / "claude-settings.json"
            claude.write_text(json.dumps({
                "model": "claude-test",
                "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:15721", "ANTHROPIC_AUTH_TOKEN": "test-only"},
            }), encoding="utf-8")
            service = DesktopService(root / "tasks", model_profile_root=root / "models", codex_executable=str(FIXTURE_CODEX))
            profile = service.handle("models.import_cc_switch", {
                "profile_id": "default",
                "config_path": str(root / "missing-config.toml"),
                "auth_path": str(root / "missing-auth.json"),
                "claude_settings_path": str(claude),
            })
            self.assertEqual("claude", profile["provider"])
            resolved = service._local_analysis({"provider": "claude", "profile_id": "claude-default", "model_id": "claude-test"})
            self.assertEqual("claude:claude-test", resolved["model_id"])
            self.assertIn("aceval.claude_bridge", resolved["model_command"])
            service.executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
