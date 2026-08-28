import json
import tempfile
import unittest
from pathlib import Path

from aceval.execution_path import EXECUTION_PATH_SPEC_API_VERSION
from aceval.iteration_brain import IterationBrain


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "evalpacks" / "csv-summary-smoke"


class IterationEvalPackGradingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _artifact(self, total_revenue):
        summary = {
            "currency": "USD",
            "total_revenue": total_revenue,
            "orders": 3,
            "by_region": {"North": 11.75, "South": 20.0},
        }
        encoded = json.dumps(summary, ensure_ascii=False).encode("utf-8")
        path = self.root / ("run-%s.json" % str(total_revenue).replace(".", "-"))
        path.write_text(
            json.dumps(
                {
                    "session": {
                        "completeness": {
                            "trace": True,
                            "output": True,
                            "usage": True,
                            "error": True,
                            "metadata": True,
                        },
                        "observation": {
                            "output": summary,
                            "artifacts": {
                                "summary.json": {
                                    "content": summary,
                                    "mime_type": "application/json",
                                    "size": len(encoded),
                                }
                            },
                            "pre_state": {"input.csv": {"content": "fixture"}},
                            "post_state": {
                                "input.csv": {"content": "fixture"},
                                "summary.json": {"content": summary},
                            },
                            "trace": [
                                {"kind": "tool_call", "name": "read_file", "payload": {"path": "input.csv"}},
                                {"kind": "tool_result", "name": "read_file", "payload": {"ok": True}},
                                {"kind": "tool_call", "name": "write_file", "payload": {"path": "summary.json"}},
                                {"kind": "tool_result", "name": "write_file", "payload": {"ok": True}},
                            ],
                            "usage": {"total_tokens": 20},
                            "metadata": {},
                            "error": None,
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    @staticmethod
    def _case():
        return {
            "id": "currency-values-dev",
            "prompt": "Create summary.json",
            "metadata": {
                "expectation_mode": "evalpack_graders",
                "evalpack_scenario_id": "currency-values-dev",
                "evalpack_lifecycle": "legacy",
                "aceval_test": {
                    "oracle_ready": True,
                    "oracle_trust": "deterministic",
                    "executable": True,
                    "needs_user_input": False,
                },
            },
        }

    def _analyze(self, total_revenue, *, path_specs=None):
        case = self._case()
        return IterationBrain(None, self.root / ("analysis-%s" % total_revenue)).analyze(
            task_id="task",
            iteration=0,
            goal="Create the correct summary",
            standards=("match the frozen EvalPack",),
            cases=(case,),
            primary_batch={
                "purpose": "evaluation",
                "environment_contract_hash": "sha256:environment",
                "cases": [
                    {
                        "case_id": case["id"],
                        "status": "completed",
                        "artifact": self._artifact(total_revenue),
                    }
                ],
            },
            path_specs=path_specs or {},
            evalpack_ref=str(PACK),
            editable_resource_inventory=("SKILL.md",),
            comparison_context_hash="sha256:comparison",
        )

    def test_desktop_iteration_brain_executes_all_selected_evalpack_graders(self):
        decision = self._analyze(31.75)

        self.assertEqual("verify_passes", decision["next_action"])
        evidence = json.loads(
            Path(decision["analysis_artifacts"]["evidence_bundle"]).read_text(encoding="utf-8")
        )["cases"][0]
        formal = evidence["formal_grading"]
        self.assertEqual("pass", formal["status"])
        self.assertEqual(
            [
                "summary-artifact",
                "output-schema",
                "currency",
                "total-revenue",
                "order-count",
                "region-totals",
                "workspace-policy",
            ],
            formal["grader_ids"],
        )
        self.assertTrue(all(item["status"] == "pass" for item in formal["grades"]))
        dimensions = decision["attempt_verdicts"][0]["dimensions"]
        self.assertTrue(any(item["grader_id"].startswith("output-schema/") for item in dimensions))
        self.assertTrue(any(item["dimension"].startswith("safety_side_effect:") for item in dimensions))

    def test_evalpack_hard_failure_authorizes_only_the_failed_trusted_case(self):
        decision = self._analyze(0.0)

        self.assertEqual("await_user_confirmation", decision["next_action"])
        self.assertEqual(["currency-values-dev"], decision["failed_case_ids"])
        self.assertEqual(["currency-values-dev"], decision["authorizable_failure_case_ids"])
        self.assertTrue(decision["requires_user_confirmation"])
        verdict = decision["attempt_verdicts"][0]
        self.assertEqual("fail", verdict["status"])
        self.assertTrue(
            any(item["hard"] and item["status"] == "fail" for item in verdict["dimensions"])
        )

    def test_formal_pass_cannot_hide_a_hard_path_failure(self):
        case_id = self._case()["id"]
        decision = self._analyze(
            31.75,
            path_specs={
                case_id: {
                    "api_version": EXECUTION_PATH_SPEC_API_VERSION,
                    "trace_completeness_required": True,
                    "steps": [
                        {
                            "id": "required-review",
                            "label": "必须执行独立复核",
                            "kind": "required",
                            "match": {"tool_name": "independent_review"},
                            "after": [],
                        }
                    ],
                }
            },
        )

        self.assertEqual("await_user_confirmation", decision["next_action"])
        self.assertEqual([case_id], decision["failed_case_ids"])
        self.assertEqual([case_id], decision["authorizable_failure_case_ids"])
        evidence = json.loads(
            Path(decision["analysis_artifacts"]["evidence_bundle"]).read_text(
                encoding="utf-8"
            )
        )["cases"][0]
        self.assertEqual("pass", evidence["formal_grading"]["status"])
        self.assertEqual("fail", evidence["path_conformance"]["status"])

    def test_draft_evalpack_case_cannot_authorize_a_skill_change(self):
        case = self._case()
        case["metadata"]["evalpack_lifecycle"] = "draft"
        decision = IterationBrain(None, self.root / "analysis-draft").analyze(
            task_id="task",
            iteration=0,
            goal="Create the correct summary",
            standards=("match the reviewed EvalPack",),
            cases=(case,),
            primary_batch={
                "purpose": "evaluation",
                "environment_contract_hash": "sha256:environment",
                "cases": [
                    {
                        "case_id": case["id"],
                        "status": "completed",
                        "artifact": self._artifact(0.0),
                    }
                ],
            },
            evalpack_ref=str(PACK),
            editable_resource_inventory=("SKILL.md",),
            comparison_context_hash="sha256:comparison",
        )

        self.assertEqual("needs_evidence", decision["next_action"])
        self.assertEqual([], decision["optimization_eligible_case_ids"])
        self.assertEqual([], decision["authorizable_failure_case_ids"])

    def test_evalpack_hash_drift_is_not_evaluable(self):
        case = self._case()
        case["metadata"]["evalpack_pack_hash"] = "sha256:" + "0" * 64
        decision = IterationBrain(None, self.root / "analysis-pack-drift").analyze(
            task_id="task",
            iteration=0,
            goal="Create the correct summary",
            standards=("match the frozen EvalPack",),
            cases=(case,),
            primary_batch={
                "purpose": "evaluation",
                "environment_contract_hash": "sha256:environment",
                "cases": [
                    {
                        "case_id": case["id"],
                        "status": "completed",
                        "artifact": self._artifact(31.75),
                    }
                ],
            },
            evalpack_ref=str(PACK),
            editable_resource_inventory=("SKILL.md",),
            comparison_context_hash="sha256:comparison",
        )

        self.assertEqual("needs_evidence", decision["next_action"])
        self.assertEqual([case["id"]], decision["not_evaluable_case_ids"])
        evidence = json.loads(
            Path(decision["analysis_artifacts"]["evidence_bundle"]).read_text(
                encoding="utf-8"
            )
        )["cases"][0]
        self.assertEqual("not_evaluable", evidence["formal_grading"]["status"])


if __name__ == "__main__":
    unittest.main()
