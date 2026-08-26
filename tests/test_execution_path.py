import unittest

from aceval.execution_path import (
    EXECUTION_PATH_SPEC_API_VERSION,
    ExecutionPathSpec,
    evaluate_trace_conformance,
)


def spec():
    return ExecutionPathSpec.from_mapping(
        {
            "api_version": EXECUTION_PATH_SPEC_API_VERSION,
            "steps": [
                {"id": "read-skill", "label": "Read instructions", "kind": "required", "match": {"event_type": "tool_call", "contains": "SKILL.md"}},
                {"id": "inspect", "label": "Inspect change", "kind": "required", "after": ["read-skill"], "match": {"tool_name": "shell", "contains": "git diff"}},
                {"id": "test-a", "label": "Run checker A", "kind": "alternative", "alternative_group": "validation", "after": ["inspect"], "match": {"contains": "npm test"}},
                {"id": "test-b", "label": "Run checker B", "kind": "alternative", "alternative_group": "validation", "after": ["inspect"], "match": {"contains": "eslint"}},
                {"id": "push", "label": "Do not push", "kind": "forbidden", "match": {"contains": "git push"}},
            ],
        }
    )


class ExecutionPathTests(unittest.TestCase):
    def test_semantic_path_passes_with_one_alternative(self):
        trace = [
            {"kind": "tool_call", "tool": "read", "payload": {"path": "src/SKILL.md"}},
            {"kind": "tool_call", "tool": "shell", "payload": {"command": "git diff master...HEAD"}},
            {"kind": "tool_call", "tool": "shell", "payload": {"command": "npm test"}},
        ]
        result = evaluate_trace_conformance(spec(), trace, trace_complete=True)
        self.assertEqual("pass", result["status"])
        self.assertEqual(1.0, result["coverage"])

    def test_incomplete_trace_is_not_skill_failure(self):
        result = evaluate_trace_conformance(spec(), [], trace_complete=False)
        self.assertEqual("not_evaluable", result["status"])
        self.assertFalse(result["skill_patch_authorized"])

    def test_missing_order_and_forbidden_actions_fail(self):
        trace = [
            {"kind": "tool_call", "tool": "shell", "payload": {"command": "git diff; git push"}},
            {"kind": "tool_call", "tool": "read", "payload": {"path": "SKILL.md"}},
        ]
        result = evaluate_trace_conformance(spec(), trace, trace_complete=True)
        self.assertEqual("fail", result["status"])
        self.assertIn("forbidden_observed", {item["type"] for item in result["violations"]})
        self.assertIn("ordering", {item["type"] for item in result["violations"]})


if __name__ == "__main__":
    unittest.main()
