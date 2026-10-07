import json
import tempfile
import unittest
from pathlib import Path

from aceval.iteration_kernel import IterationKernel
from aceval.kernel_contracts import (
    KERNEL_CONFIG_API_VERSION,
    KERNEL_INPUT_API_VERSION,
    KernelConfig,
    KernelInput,
)


class IterationSnapshotTests(unittest.TestCase):
    def test_session_log_read_model_exposes_remote_attempt_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
            kernel = IterationKernel(root / "tasks")
            config = KernelConfig.from_mapping(
                {
                    "api_version": KERNEL_CONFIG_API_VERSION,
                    "skill_repository": {
                        "ssh_url": "ssh://git@example/skill.git",
                        "branch": "feature/eval",
                        "local_path": str(skill),
                        "mount_path": "/workspace/skill",
                    },
                    "code_repository": None,
                    "local_analysis": {
                        "model_command": ["unused"],
                        "model_id": "test-model",
                    },
                    "remote_agent": {"profile_path": str(root / "profile.json")},
                }
            )
            user_input = KernelInput.from_mapping(
                {
                    "api_version": KERNEL_INPUT_API_VERSION,
                    "skill_name": "skill",
                    "goal": "Return grounded output",
                }
            )
            kernel.create_task(config, user_input, task_id="snapshot-attempts")
            artifact = (
                kernel.store.task_dir("snapshot-attempts")
                / "iterations"
                / "iteration-000"
                / "runs"
                / "evaluation"
                / "case-1"
                / "attempt-002.json"
            )
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}\n", encoding="utf-8")
            batch_path = artifact.parents[3] / "batches" / "evaluation.json"
            batch_path.parent.mkdir(parents=True, exist_ok=True)
            batch_path.write_text(
                json.dumps(
                    {
                        "purpose": "evaluation",
                        "cases": [
                            {
                                "case_id": "case-1",
                                "case_title": "Grounded output",
                                "session_id": "session-2",
                                "status": "completed",
                                "artifact": str(artifact),
                                "attempt_number": 2,
                                "retry_reason": "temporary gateway failure",
                                "attempts": [
                                    {
                                        "attempt_number": 1,
                                        "session_id": "session-1",
                                        "status": "failed",
                                        "retry_reason": "temporary gateway failure",
                                    },
                                    {
                                        "attempt_number": 2,
                                        "session_id": "session-2",
                                        "status": "completed",
                                    },
                                ],
                            },
                            {
                                "case_id": "case-before-log",
                                "case_title": "Gateway creation failure",
                                "session_id": None,
                                "status": "failed",
                                "artifact": None,
                                "attempt_number": 1,
                                "retry_reason": "gateway rejected session creation",
                                "attempts": [
                                    {
                                        "attempt_number": 1,
                                        "session_id": None,
                                        "status": "failed",
                                        "retry_reason": "gateway rejected session creation",
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            logs = kernel.snapshot("snapshot-attempts")["iterations"][0][
                "session_logs"
            ]
            self.assertEqual(2, len(logs))
            log = logs[0]
            self.assertEqual(2, log["attempt_number"])
            self.assertEqual(2, len(log["attempts"]))
            self.assertEqual(
                "temporary gateway failure", log["attempts"][0]["retry_reason"]
            )
            self.assertEqual("temporary gateway failure", log["retry_reason"])
            failed_before_log = logs[1]
            self.assertIsNone(failed_before_log["artifact"])
            self.assertEqual(1, len(failed_before_log["attempts"]))
            self.assertIn("gateway rejected", failed_before_log["retry_reason"])


if __name__ == "__main__":
    unittest.main()
