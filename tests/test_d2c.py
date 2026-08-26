import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from aceval.d2c import (
    D2C_DRIVER_API_VERSION,
    D2C_PROFILE_API_VERSION,
    D2C_REQUEST_API_VERSION,
    D2CBrowserProfile,
    D2CError,
    D2CValidationRequest,
    check_d2c_profile,
    validate_d2c,
)


FAKE_DRIVER = r'''
import json
import pathlib
import sys
request = json.load(sys.stdin)
output = pathlib.Path(request["output_dir"])
(output / "screenshot.png").write_bytes(b"png")
(output / "dom.html").write_text("<html><title>Fixture</title></html>")
(output / "console.json").write_text("[]\n")
(output / "network.json").write_text("[]\n")
success = request.get("expected_title") in (None, "Fixture")
result = {
  "api_version": "aceval.d2c-browser-driver/v1",
  "success": success,
  "title": "Fixture",
  "console_errors": 0,
}
(output / "driver-result.json").write_text(json.dumps(result))
json.dump(result, sys.stdout)
'''


class D2CTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.driver = self.root / "driver.py"
        self.driver.write_text(FAKE_DRIVER, encoding="utf-8")
        self.profile = D2CBrowserProfile(
            api_version=D2C_PROFILE_API_VERSION,
            name="test-browser",
            node_executable=sys.executable,
            chrome_executable=sys.executable,
            driver_path=str(self.driver),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, title="Fixture"):
        return D2CValidationRequest(
            api_version=D2C_REQUEST_API_VERSION,
            case_id="home-page",
            url="http://127.0.0.1:4173/",
            candidate_commit="a" * 40,
            expected_title=title,
            actions=({"type": "click", "selector": "#button"},),
        )

    def test_profile_check_builds_environment_fingerprint(self):
        report = check_d2c_profile(self.profile)
        self.assertTrue(report["ready"])
        self.assertTrue(report["environment_fingerprint"].startswith("sha256:"))

    def test_validation_writes_content_addressed_receipt_and_evidence(self):
        output = self.root / "artifacts"
        receipt = validate_d2c(self.profile, self.request(), output)
        self.assertEqual("succeeded", receipt["status"])
        self.assertEqual("a" * 40, receipt["candidate_commit"])
        self.assertIn("screenshot.png", receipt["artifacts"])
        self.assertTrue((output / "receipt.json").is_file())
        self.assertEqual(D2C_DRIVER_API_VERSION, receipt["driver_summary"]["api_version"])

    def test_node_worker_enables_websocket_compatibility_without_third_party_packages(self):
        from dataclasses import replace
        from unittest.mock import patch

        output = self.root / "captured"
        captured = {}

        def run(argv, **kwargs):
            captured["argv"] = tuple(argv)
            output.mkdir(parents=True, exist_ok=True)
            (output / "driver-result.json").write_text("{}", encoding="utf-8")
            payload = {"api_version": D2C_DRIVER_API_VERSION, "success": True}
            return type("Completed", (), {"returncode": 0, "stdout": json.dumps(payload).encode(), "stderr": b""})()

        builtin_profile = replace(self.profile, driver_path=None)
        with patch("aceval.d2c.check_d2c_profile", return_value={"ready": True, "environment_fingerprint": "sha256:test", "missing": []}), patch("aceval.d2c.subprocess.run", side_effect=run):
            receipt = validate_d2c(builtin_profile, self.request(), output)
        self.assertEqual("--experimental-websocket", captured["argv"][1])
        self.assertEqual("succeeded", receipt["status"])

    def test_packaged_electron_runtime_can_be_used_as_the_node_worker(self):
        from unittest.mock import patch

        captured = []

        def run(argv, **kwargs):
            captured.append(dict(kwargs.get("env") or {}))
            return type("Completed", (), {"returncode": 0, "stdout": b"v22.0.0", "stderr": b""})()

        with patch.dict(
            os.environ,
            {
                "ACEVAL_D2C_NODE_EXECUTABLE": sys.executable,
                "ACEVAL_D2C_USE_ELECTRON_NODE": "1",
            },
            clear=False,
        ), patch("aceval.d2c.subprocess.run", side_effect=run):
            report = check_d2c_profile(self.profile)

        self.assertTrue(report["ready"])
        self.assertEqual("1", captured[0]["ELECTRON_RUN_AS_NODE"])
        self.assertNotIn("ELECTRON_RUN_AS_NODE", captured[1])

    def test_title_mismatch_is_candidate_failure_not_infrastructure(self):
        receipt = validate_d2c(self.profile, self.request("Other"), self.root / "failed")
        self.assertEqual("candidate_failed", receipt["status"])
        self.assertEqual([], receipt["infrastructure_errors"])

    def test_remote_url_is_rejected_by_local_provider(self):
        with self.assertRaises(D2CError):
            D2CValidationRequest(
                api_version=D2C_REQUEST_API_VERSION,
                case_id="unsafe",
                url="https://example.com/",
                candidate_commit="b" * 40,
            )


if __name__ == "__main__":
    unittest.main()
