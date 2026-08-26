import json
import tempfile
import unittest
from pathlib import Path

from aceval.agent_runtime import ModelReply
from aceval.evidence_analysis import CrossCaseAnalyzer
from aceval.execution_path import EXECUTION_PATH_SPEC_API_VERSION


class CountingModel:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append(messages)
        return ModelReply(content=json.dumps(self.payload), usage={"input_tokens": 100, "output_tokens": 20})


def path_spec():
    return {
        "api_version": EXECUTION_PATH_SPEC_API_VERSION,
        "trace_completeness_required": True,
        "steps": [
            {"id": "load", "label": "Load Skill", "kind": "required", "match": {"contains": "SKILL.md"}, "after": []}
        ],
    }


class EvidenceAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def artifact(self, name, output, *, trace=True):
        path = self.root / (name + ".json")
        path.write_text(
            json.dumps(
                {
                    "session": {
                        "completeness": {"trace": trace, "output": True},
                        "observation": {
                            "output": output,
                            "trace": [{"kind": "tool_call", "name": "read", "path": "SKILL.md"}],
                            "metadata": {},
                            "usage": {"total_tokens": 9},
                            "error": None,
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    def test_exact_expectation_is_model_free_and_pass_is_verified_once(self):
        model = CountingModel({})
        cases = ({"id": "exact", "prompt": "Do it", "expected_output": "ok"},)
        primary = {"cases": [{"case_id": "exact", "status": "completed", "artifact": self.artifact("primary", "ok")}]}
        first = CrossCaseAnalyzer(model).analyze(
            task_id="task", iteration=0, goal="goal", standards=("correct",), cases=cases,
            primary_batch=primary, path_specs={"exact": path_spec()},
        )
        self.assertEqual("verify_passes", first["next_action"])
        verification = {"cases": [{"case_id": "exact", "status": "completed", "artifact": self.artifact("verify", "ok")}]}
        second = CrossCaseAnalyzer(model).analyze(
            task_id="task", iteration=0, goal="goal", standards=("correct",), cases=cases,
            primary_batch=primary, path_specs={"exact": path_spec()}, verification_batch=verification,
        )
        self.assertEqual("converged", second["next_action"])
        self.assertEqual(["exact"], second["stable_pass_case_ids"])
        self.assertEqual([], model.calls)

    def test_subjective_cases_use_one_cross_case_call_but_cannot_reverse_exact_failure(self):
        cases = (
            {"id": "exact", "prompt": "Exact", "expected_output": "right"},
            {"id": "subjective", "prompt": "Explain"},
        )
        model = CountingModel(
            {
                "case_results": [
                    {"case_id": "exact", "status": "pass", "verification_status": "not_run", "reason": "wrong override", "evidence_refs": []},
                    {"case_id": "subjective", "status": "fail", "verification_status": "not_run", "reason": "unsupported", "evidence_refs": []},
                ],
                "failure_clusters": [{"id": "quality", "case_ids": ["exact", "subjective"], "root_cause": "weak evidence", "skill_change_authorized": True}],
                "conflicts": [],
                "proposed_changes": [{"target": "SKILL.md", "change": "require evidence", "why": "quality", "case_ids": ["exact", "subjective"]}],
                "target_scope": ["SKILL.md"],
            }
        )
        batch = {
            "cases": [
                {"case_id": "exact", "status": "completed", "artifact": self.artifact("exact", "wrong")},
                {"case_id": "subjective", "status": "completed", "artifact": self.artifact("subjective", "answer")},
            ]
        }
        decision = CrossCaseAnalyzer(model, max_evidence_chars_per_case=500).analyze(
            task_id="task", iteration=0, goal="goal", standards=("correct",), cases=cases,
            primary_batch=batch, path_specs={"exact": path_spec(), "subjective": path_spec()},
        )
        self.assertEqual(1, len(model.calls))
        self.assertEqual("fail", decision["case_results"][0]["status"])
        self.assertFalse(decision["token_economy"]["full_logs_sent"])

    def test_remote_failure_requests_evidence_instead_of_authorizing_skill_repair(self):
        decision = CrossCaseAnalyzer(None).analyze(
            task_id="task",
            iteration=0,
            goal="goal",
            standards=("correct",),
            cases=({"id": "infra", "prompt": "Do it", "expected_output": "ok"},),
            primary_batch={"cases": [{"case_id": "infra", "status": "failed", "error": "gateway timeout"}]},
        )
        self.assertEqual("needs_evidence", decision["next_action"])
        self.assertEqual([], decision["failed_case_ids"])
        self.assertEqual(["infra"], decision["not_evaluable_case_ids"])

    def test_failed_pass_verification_becomes_an_optimizable_flake_cluster(self):
        cases = ({"id": "flaky", "prompt": "Do it", "expected_output": "ok"},)
        primary = {"cases": [{"case_id": "flaky", "status": "completed", "artifact": self.artifact("flaky-primary", "ok")}]}
        verification = {"cases": [{"case_id": "flaky", "status": "completed", "artifact": self.artifact("flaky-verify", "wrong")}]}
        decision = CrossCaseAnalyzer(None).analyze(
            task_id="task", iteration=0, goal="goal", standards=("correct",), cases=cases,
            primary_batch=primary, verification_batch=verification,
        )
        self.assertEqual("await_user_confirmation", decision["next_action"])
        self.assertEqual(["flaky"], decision["flaky_case_ids"])
        self.assertTrue(any(cluster["id"] == "unstable-pass-verification" for cluster in decision["failure_clusters"]))

    def test_multi_file_scope_is_authorized_only_from_editable_inventory(self):
        payload = {
            "case_results": [{"case_id": "script", "status": "fail", "verification_status": "not_run", "reason": "collector truncates evidence", "evidence_refs": []}],
            "failure_clusters": [{"id": "collector", "case_ids": ["script"], "root_cause": "collector truncation", "skill_change_authorized": True}],
            "conflicts": [],
            "proposed_changes": [{"target": "scripts/collectors/group_b_js.py", "change": "remove the incorrect truncation", "why": "preserve evidence", "case_ids": ["script"]}],
            "target_scope": ["scripts/collectors/group_b_js.py", "workflow/fast/stage-3-grade.md"],
        }
        cases = ({"id": "script", "prompt": "Grade alert", "expected_output": "high"},)
        batch = {"cases": [{"case_id": "script", "status": "completed", "artifact": self.artifact("script", "low")}]}

        accepted = CrossCaseAnalyzer(CountingModel(payload)).analyze(
            task_id="task", iteration=0, goal="goal", standards=("correct",), cases=cases,
            primary_batch=batch,
            editable_resource_inventory=("SKILL.md", "scripts/collectors/group_b_js.py", "workflow/fast/stage-3-grade.md"),
        )
        rejected = CrossCaseAnalyzer(CountingModel(payload)).analyze(
            task_id="task", iteration=0, goal="goal", standards=("correct",), cases=cases,
            primary_batch=batch,
        )

        self.assertEqual("await_user_confirmation", accepted["next_action"])
        self.assertEqual("needs_evidence", rejected["next_action"])
        self.assertIn("not an editable existing", rejected["intervention_blocker"])

    def test_semantic_without_skill_baseline_requires_explicit_failure_for_incremental_value(self):
        payload = {
            "case_results": [{"case_id": "semantic", "status": "pass", "verification_status": "pass", "reason": "grounded", "evidence_refs": []}],
            "without_skill_baseline_case_results": [{"case_id": "semantic", "status": "fail", "reason": "ungrounded", "evidence_refs": []}],
            "failure_clusters": [],
            "conflicts": [],
            "proposed_changes": [],
            "target_scope": [],
        }
        cases = ({"id": "semantic", "prompt": "Summarize evidence", "metadata": {"expectation_mode": "semantic"}},)
        primary = {"cases": [{"case_id": "semantic", "status": "completed", "artifact": self.artifact("candidate-semantic", "grounded answer")} ]}
        baseline = {"cases": [{"case_id": "semantic", "status": "completed", "artifact": self.artifact("baseline-semantic", "generic answer")} ]}

        decision = CrossCaseAnalyzer(CountingModel(payload)).analyze(
            task_id="task", iteration=0, goal="goal", standards=("grounded",), cases=cases,
            primary_batch=primary, comparison_baseline_batch=baseline,
        )

        self.assertEqual(["semantic"], decision["incremental_value_case_ids"])
        self.assertEqual("fail", decision["without_skill_baseline_case_results"][0]["status"])

    def test_not_evaluable_baseline_is_not_counted_as_incremental_value(self):
        payload = {
            "case_results": [{"case_id": "semantic", "status": "pass", "verification_status": "pass", "reason": "grounded", "evidence_refs": []}],
            "without_skill_baseline_case_results": [{"case_id": "semantic", "status": "not_evaluable", "reason": "insufficient evidence", "evidence_refs": []}],
            "failure_clusters": [],
            "conflicts": [],
            "proposed_changes": [],
            "target_scope": [],
        }
        cases = ({"id": "semantic", "prompt": "Summarize evidence"},)
        primary = {"cases": [{"case_id": "semantic", "status": "completed", "artifact": self.artifact("candidate-uncertain", "answer")} ]}
        baseline = {"cases": [{"case_id": "semantic", "status": "completed", "artifact": self.artifact("baseline-uncertain", "answer")} ]}

        decision = CrossCaseAnalyzer(CountingModel(payload)).analyze(
            task_id="task", iteration=0, goal="goal", standards=("grounded",), cases=cases,
            primary_batch=primary, comparison_baseline_batch=baseline,
        )

        self.assertEqual([], decision["incremental_value_case_ids"])


if __name__ == "__main__":
    unittest.main()
