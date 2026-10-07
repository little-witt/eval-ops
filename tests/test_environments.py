import tempfile
import unittest
from pathlib import Path

from aceval.code_review import repository_verify_blueprint
from aceval.environment_contracts import (
    VALIDATION_REQUEST_API_VERSION,
    ValidationRequest,
    ValidationStatus,
    create_candidate_bundle,
)
from aceval.environments import LocalDockerProvider, ProcessResult


DIGEST = "sha256:" + "b" * 64


class FakeRunner:
    def __init__(self, validation_exit_code=0, unavailable=False):
        self.validation_exit_code = validation_exit_code
        self.unavailable = unavailable
        self.calls = []

    def run(self, argv, *, timeout_seconds, cwd=None):
        values = tuple(argv)
        self.calls.append(values)
        if self.unavailable and values[1:2] == ("version",):
            raise FileNotFoundError("docker")
        stdout = b""
        stderr = b""
        returncode = 0
        if values[1:2] == ("version",):
            stdout = b"27.1.0\n"
        elif values[1:3] == ("image", "inspect"):
            stdout = (DIGEST + "\n").encode()
        elif values[1:2] == ("create",):
            stdout = b"container-id\n"
        elif values[1:2] == ("exec",) and "--self-check" not in values:
            returncode = self.validation_exit_code
            if returncode:
                stderr = b'{"ready": false}'
            else:
                stdout = b'{"api_version":"repository.verify/v1","ready":true}'
        return ProcessResult(values, returncode, stdout, stderr, 2.0)


class EnvironmentProviderTests(unittest.TestCase):
    def request(self, root, runner):
        base = "1" * 40
        head = "2" * 40
        bundle = create_candidate_bundle(
            root,
            subject_hash="sha256:" + "c" * 64,
            producer_run_id="run-1",
            base_commit=base,
            result_commit=head,
        )
        blueprint = repository_verify_blueprint(DIGEST, base_commit=base, head_commit=head)
        return ValidationRequest(
            api_version=VALIDATION_REQUEST_API_VERSION,
            run_id="validation-1",
            suite_hash="sha256:" + "d" * 64,
            scenario_id="sql-injection",
            candidate=bundle,
            blueprint=blueprint,
            grader_contract="code_review_findings_v1@1.0.0",
        )

    def test_successful_lifecycle_is_offline_readonly_and_cleanup_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "repo")
            root.mkdir()
            Path(root, "x.py").write_text("x = 1\n", encoding="utf-8")
            runner = FakeRunner()
            receipt = LocalDockerProvider(runner).validate_once(self.request(root, runner))
        self.assertEqual(ValidationStatus.SUCCEEDED, receipt.status)
        create = next(call for call in runner.calls if call[1] == "create")
        self.assertIn("none", create)
        self.assertIn("--read-only", create)
        self.assertTrue(any("readonly" in item for item in create))
        self.assertEqual("rm", runner.calls[-1][1])

    def test_candidate_failure_is_not_an_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "repo")
            root.mkdir()
            Path(root, "x.py").write_text("x = 1\n", encoding="utf-8")
            runner = FakeRunner(validation_exit_code=1)
            receipt = LocalDockerProvider(runner).validate_once(self.request(root, runner))
        self.assertEqual(ValidationStatus.CANDIDATE_FAILED, receipt.status)
        self.assertEqual((), receipt.infrastructure_errors)


if __name__ == "__main__":
    unittest.main()
