import tempfile
import unittest
from pathlib import Path

from aceval.desktop_service import DESKTOP_SERVICE_API_VERSION, DesktopService


class DesktopServiceTests(unittest.TestCase):
    def test_bootstrap_reports_real_local_capabilities_without_opening_a_port(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DesktopService(Path(directory) / "tasks")
            value = service.handle("system.bootstrap", {})
            self.assertEqual(DESKTOP_SERVICE_API_VERSION, value["api_version"])
            self.assertTrue(value["capabilities"]["skill_harness"])
            self.assertTrue(value["capabilities"]["d2c_browser_worker"])
            self.assertTrue(value["environment_health"]["kernel"]["ready"])
            self.assertIn("git", value["environment_health"])
            self.assertEqual([], service.handle("tasks.list", {})["tasks"])
            service.executor.shutdown(wait=True)

    def test_unknown_renderer_method_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DesktopService(Path(directory) / "tasks")
            with self.assertRaises(ValueError):
                service.handle("shell.execute", {"command": "whoami"})
            service.executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
