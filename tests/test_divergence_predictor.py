import unittest

from aceval.divergence_predictor import (
    DIVERGENCE_REPORT_API_VERSION,
    DivergenceError,
    analyze_divergence,
    fingerprint_trajectory,
)
from aceval.execution_path import EXECUTION_PATH_SPEC_API_VERSION, ExecutionPathSpec


def path_spec():
    return ExecutionPathSpec.from_mapping(
        {
            "api_version": EXECUTION_PATH_SPEC_API_VERSION,
            "steps": [
                {
                    "id": "read-skill",
                    "label": "Read Skill",
                    "kind": "required",
                    "match": {"event_type": "tool_call", "contains": "SKILL.md"},
                },
                {
                    "id": "validate",
                    "label": "Validate result",
                    "kind": "required",
                    "match": {"tool_name": "shell", "contains": "pytest"},
                },
            ],
        }
    )


def trace(*commands):
    return [
        {
            "kind": "tool_call",
            "seq": index,
            "tool": "read" if "SKILL.md" in command else "shell",
            "payload": {"command": command},
        }
        for index, command in enumerate(commands)
    ]


class DivergencePredictorTests(unittest.TestCase):
    def test_identical_paths_have_no_divergence(self):
        attempts = [
            {"attempt_id": "path-1", "trace": trace("cat SKILL.md", "pytest"), "outcome": "pass", "usage": {"total_tokens": 100}},
            {"attempt_id": "path-2", "trace": trace("cat SKILL.md", "pytest"), "outcome": "pass", "usage": {"total_tokens": 100}},
            {"attempt_id": "path-3", "trace": trace("cat SKILL.md", "pytest"), "outcome": "pass", "usage": {"total_tokens": 100}},
        ]
        result = analyze_divergence("case-1", attempts, path_spec=path_spec())
        self.assertEqual(DIVERGENCE_REPORT_API_VERSION, result.api_version)
        self.assertEqual(0.0, result.divergence_score)
        self.assertEqual("none", result.predicted_defect_surface)
        self.assertEqual("low", result.evidence_confidence)
        self.assertIsNone(result.first_tool_divergence_index)

    def test_outcome_and_path_divergence_are_detected(self):
        attempts = [
            {"attempt_id": "path-1", "trace": trace("cat SKILL.md", "pytest"), "outcome": "pass", "usage": {"total_tokens": 100}},
            {"attempt_id": "path-2", "trace": trace("cat SKILL.md", "echo skipped"), "outcome": "fail", "usage": {"total_tokens": 200}},
            {"attempt_id": "path-3", "trace": trace("cat SKILL.md", "pytest"), "outcome": "pass", "usage": {"total_tokens": 100}},
        ]
        result = analyze_divergence("case-2", attempts, path_spec=path_spec())
        self.assertGreater(result.outcome_disagreement, 0.0)
        self.assertGreater(result.checkpoint_divergence, 0.0)
        self.assertGreater(result.tool_sequence_divergence, 0.0)
        self.assertGreater(result.cost_variance, 0.0)
        self.assertGreater(result.divergence_score, 0.0)
        self.assertEqual(1, result.first_tool_divergence_index)
        self.assertEqual("moderate", result.evidence_confidence)

    def test_fingerprint_marks_incomplete_trace_without_authorizing_a_verdict(self):
        fingerprint = fingerprint_trajectory(
            "path-1", [], outcome="not_evaluable", path_spec=path_spec(), trace_complete=False
        )
        self.assertEqual("not_evaluable", fingerprint.conformance_status)
        self.assertEqual((), fingerprint.required_step_ids)
        self.assertTrue(fingerprint.fingerprint_hash.startswith("sha256:"))

    def test_report_hash_is_stable(self):
        attempts = [
            {"attempt_id": "path-1", "trace": trace("cat SKILL.md", "pytest"), "outcome": "pass"},
            {"attempt_id": "path-2", "trace": trace("cat SKILL.md", "echo skipped"), "outcome": "fail"},
        ]
        first = analyze_divergence("case-3", attempts, path_spec=path_spec())
        second = analyze_divergence("case-3", attempts, path_spec=path_spec())
        self.assertEqual(first.report_hash, second.report_hash)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_requires_two_attempts_and_valid_weights(self):
        with self.assertRaises(DivergenceError):
            analyze_divergence("case-4", [{"attempt_id": "only", "trace": [], "outcome": "pass"}])
        with self.assertRaises(DivergenceError):
            analyze_divergence(
                "case-4",
                [
                    {"attempt_id": "one", "trace": [], "outcome": "pass"},
                    {"attempt_id": "two", "trace": [], "outcome": "pass"},
                ],
                weights=(0.0, 0.0, 0.0, 0.0),
            )


if __name__ == "__main__":
    unittest.main()
