import unittest

from aceval.execution_path import (
    EXECUTION_PATH_SPEC_API_VERSION,
    EXECUTION_PATH_SPEC_V3_API_VERSION,
    PATH_ANALYSIS_API_VERSION,
    ExecutionPathSpec,
    evaluate_trace_conformance,
    resolve_execution_path,
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
    def test_v3_nodes_and_edges_are_read_only_normalized_to_v1(self):
        source = {
            "api_version": PATH_ANALYSIS_API_VERSION,
            "nodes": [
                {
                    "step_id": "load",
                    "requiredness": "required",
                    "action": "Load Skill",
                    "tool": "read_file",
                },
                {
                    "step_id": "inspect",
                    "requiredness": "required",
                    "action": "Inspect change",
                    "match": {"tool_name": "shell", "contains": "git diff"},
                    "predecessors": ["load"],
                },
            ],
            "edges": [{"from": "load", "to": "inspect", "kind": "success"}],
        }
        spec = ExecutionPathSpec.from_mapping(source)
        self.assertEqual(EXECUTION_PATH_SPEC_API_VERSION, spec.api_version)
        self.assertEqual(("load", "inspect"), tuple(step.id for step in spec.steps))
        self.assertEqual("required", spec.steps[0].kind)
        self.assertEqual("read_file", spec.steps[0].match["tool_name"])
        self.assertEqual(("load",), spec.steps[1].after)
        # Normalization must not mutate the authored V3 document.
        self.assertIn("nodes", source)
        self.assertNotIn("id", source["nodes"][0])

    def test_v3_execution_path_api_version_is_accepted_but_unknown_versions_fail(self):
        spec = ExecutionPathSpec.from_mapping(
            {
                "api_version": EXECUTION_PATH_SPEC_V3_API_VERSION,
                "nodes": [
                    {
                        "step_id": "one",
                        "requiredness": "required",
                        "label": "one",
                        "match": {"contains": "one"},
                    }
                ],
            }
        )
        self.assertEqual(EXECUTION_PATH_SPEC_API_VERSION, spec.api_version)
        with self.assertRaises(ValueError):
            ExecutionPathSpec.from_mapping(
                {
                    "api_version": "aceval.path-analysis/v99",
                    "nodes": [],
                }
            )

    def test_v3_only_unconditional_edges_become_v1_order_constraints(self):
        path = ExecutionPathSpec.from_mapping(
            {
                "api_version": PATH_ANALYSIS_API_VERSION,
                "nodes": [
                    {
                        "step_id": "load",
                        "requiredness": "required",
                        "action": "Load",
                        "match": {"contains": "load"},
                    },
                    {
                        "step_id": "call",
                        "requiredness": "required",
                        "action": "Call tool",
                        "match": {"contains": "call"},
                    },
                    {
                        "step_id": "retry",
                        "requiredness": "recommended",
                        "action": "Retry timeout",
                        "match": {"contains": "retry"},
                    },
                ],
                "edges": [
                    {"from": "load", "to": "call", "kind": "sequence"},
                    {
                        "from": "call",
                        "to": "retry",
                        "kind": "failure",
                        "when": "exit_code=timeout",
                    },
                ],
            }
        )
        by_id = {step.id: step for step in path.steps}
        self.assertEqual(("load",), by_id["call"].after)
        self.assertEqual((), by_id["retry"].after)
        result = evaluate_trace_conformance(
            path,
            [
                {"kind": "tool_call", "payload": {"text": "load"}},
                {"kind": "tool_call", "payload": {"text": "call"}},
            ],
            trace_complete=True,
        )
        self.assertEqual("not_evaluable", result["status"])
        self.assertFalse(result["skill_patch_authorized"])
        self.assertIn("read-only", result["reason"])

    def test_v3_edges_reject_dangling_endpoints_even_when_conditional(self):
        with self.assertRaisesRegex(ValueError, "references unknown nodes: missing"):
            ExecutionPathSpec.from_mapping(
                {
                    "api_version": PATH_ANALYSIS_API_VERSION,
                    "nodes": [
                        {
                            "step_id": "load",
                            "requiredness": "required",
                            "action": "Load",
                            "match": {"contains": "load"},
                        }
                    ],
                    "edges": [
                        {
                            "from": "load",
                            "to": "missing",
                            "kind": "failure",
                            "when": "error",
                        }
                    ],
                }
            )

    def test_missing_and_false_trace_completeness_are_distinct_and_never_inferred(self):
        complete_unknown = evaluate_trace_conformance(spec(), [{"kind": "tool_call", "tool": "read"}])
        self.assertEqual("not_evaluable", complete_unknown["status"])
        self.assertEqual("unknown", complete_unknown["trace_completeness"])
        self.assertIn("not provided", complete_unknown["reason"])

        explicitly_incomplete = evaluate_trace_conformance(
            spec(), [{"kind": "tool_call", "tool": "read"}], trace_complete=False
        )
        self.assertEqual("not_evaluable", explicitly_incomplete["status"])
        self.assertEqual("incomplete", explicitly_incomplete["trace_completeness"])
        self.assertIn("explicitly marked incomplete", explicitly_incomplete["reason"])

    def test_self_referencing_predecessor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot reference itself"):
            ExecutionPathSpec.from_mapping(
                {
                    "api_version": EXECUTION_PATH_SPEC_API_VERSION,
                    "steps": [
                        {
                            "id": "loop",
                            "label": "Loop",
                            "kind": "required",
                            "match": {"contains": "loop"},
                            "after": ["loop"],
                        }
                    ],
                }
            )

    def test_resolve_execution_path_supports_direct_spec_and_per_case_catalog(self):
        direct = spec()
        self.assertIs(direct, resolve_execution_path(direct, "case-a"))
        catalog = {
            "api_version": "aceval.kernel-execution-paths/v1",
            "paths": {
                "case-a": {
                    "api_version": EXECUTION_PATH_SPEC_API_VERSION,
                    "steps": [
                        {
                            "id": "load",
                            "label": "Load",
                            "kind": "required",
                            "match": {"contains": "SKILL.md"},
                        }
                    ],
                }
            }
        }
        resolved = resolve_execution_path(catalog, "case-a")
        self.assertIsNotNone(resolved)
        self.assertEqual("load", resolved.steps[0].id)
        self.assertIsNone(resolve_execution_path(catalog, "missing"))

    def test_direct_path_nodes_take_precedence_over_auxiliary_paths_metadata(self):
        resolved = resolve_execution_path(
            {
                "api_version": EXECUTION_PATH_SPEC_API_VERSION,
                "steps": [
                    {
                        "id": "load",
                        "label": "Load",
                        "kind": "required",
                        "match": {"contains": "SKILL.md"},
                    }
                ],
                "paths": {"display-only": {"label": "auxiliary metadata"}},
            },
            "case-a",
        )
        self.assertEqual("load", resolved.steps[0].id)

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

    def test_forbidden_command_matches_executed_argv_not_instruction_text(self):
        safety_spec = ExecutionPathSpec.from_mapping(
            {
                "api_version": EXECUTION_PATH_SPEC_API_VERSION,
                "steps": [
                    {
                        "id": "no-push",
                        "label": "Do not publish",
                        "kind": "forbidden",
                        "match": {"command_contains": "git push"},
                    }
                ],
            }
        )
        instruction_only = evaluate_trace_conformance(
            safety_spec,
            [
                {
                    "kind": "assistant_message",
                    "payload": {"content": "I will not run git push."},
                },
                {
                    "kind": "tool_call",
                    "tool": "read_file",
                    "payload": {"path": "SKILL.md"},
                },
            ],
            trace_complete=True,
        )
        self.assertEqual("pass", instruction_only["status"])

        executed = evaluate_trace_conformance(
            safety_spec,
            [
                {
                    "kind": "tool_call",
                    "tool": "process_exec",
                    "payload": {"argv": ["git", "push", "origin", "HEAD"]},
                }
            ],
            trace_complete=True,
        )
        self.assertEqual("fail", executed["status"])
        self.assertEqual("forbidden_observed", executed["violations"][0]["type"])


if __name__ == "__main__":
    unittest.main()
