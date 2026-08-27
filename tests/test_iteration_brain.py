import json
import tempfile
import unittest
from pathlib import Path

from aceval.agent_runtime import ModelReply
from aceval.iteration_brain import EvidenceQueryError, IterationBrain, LocalEvidenceQueryPort


class StagedModel:
    model_id = "forge-test-model"
    profile = "forge-test-profile"

    def __init__(self, invalid_stage=None):
        self.calls = []
        self.invalid_stage = invalid_stage

    def complete(self, messages, tools):
        stage = messages[0]["content"].split(" the ", 1)[-1].split(" stage", 1)[0]
        request = json.loads(messages[-1]["content"])
        self.calls.append((stage, request))
        if stage == self.invalid_stage:
            return ModelReply(content="not-json")
        if stage == "semantic_grading":
            return ModelReply(content=json.dumps({
                "case_results": [{"case_id": c["id"], "status": "fail", "verification_status": "not_run", "reason": "semantic defect", "evidence_refs": []} for c in request["cases"]],
                "without_skill_baseline_case_results": [],
            }), usage={"total_tokens": 7})
        if stage == "attribution":
            return ModelReply(content=json.dumps({"failure_clusters": [{"id": "c", "case_ids": [c["case_id"] for c in request["case_results"]], "root_cause": "defect", "skill_change_authorized": True}], "conflicts": []}))
        ids = request.get("case_results", [{}])[0].get("case_id") if request.get("case_results") else "c"
        return ModelReply(content=json.dumps({"proposed_changes": [{"target": "SKILL.md", "change": "fix defect", "why": "evidence", "case_ids": [ids]}], "target_scope": ["SKILL.md"]}))


class IterationBrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run = self.root / "run.json"
        self.run.write_text(json.dumps({"session": {"completeness": {"trace": True, "output": True}, "observation": {"output": "observed", "trace": [{"kind": "tool_call", "name": "read_file", "path": "SKILL.md"}], "metadata": {}, "error": None}}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **extra):
        value = {"task_id": "t", "iteration": 0, "goal": "goal", "standards": ["correct"], "cases": ({"id": "c", "prompt": "judge"},), "primary_batch": {"cases": [{"case_id": "c", "status": "completed", "artifact": str(self.run)}]}, "editable_resource_inventory": ["SKILL.md"]}
        value.update(extra)
        return value

    def test_three_independent_stages_and_windows(self):
        model = StagedModel()
        decision = IterationBrain(model, self.root / "analysis").analyze(**self.args())
        self.assertEqual(["semantic_grading", "attribution", "proposal"], [x[0] for x in model.calls])
        self.assertIn("observed", model.calls[0][1]["windows"][0]["window"]["text"])
        self.assertIn("case_aggregates", decision)
        self.assertEqual("needs_evidence", decision["next_action"])
        self.assertEqual("forge-test-model", decision["agent_call_receipts"][0]["model"])
        self.assertEqual("forge-test-profile", decision["agent_call_receipts"][0]["profile"])
        manifests = list((self.root / "analysis").glob("attempt-*/**/manifest.json"))
        self.assertEqual(4, len(manifests))

    def test_rebuild_reuses_validated_stages(self):
        first = StagedModel()
        IterationBrain(first, self.root / "analysis").analyze(**self.args())
        second = StagedModel()
        IterationBrain(second, self.root / "analysis").analyze(**self.args())
        self.assertEqual([], second.calls)

    def test_invalid_json_is_failed_and_not_validated(self):
        model = StagedModel("semantic_grading")
        with self.assertRaises(ValueError):
            IterationBrain(model, self.root / "analysis").analyze(**self.args())
        manifests = list((self.root / "analysis").glob("attempt-*/semantic_grading/manifest.json"))
        self.assertEqual("failed", json.loads(manifests[0].read_text())["status"])
        self.assertFalse(json.loads(manifests[0].read_text())["artifact_validated"])

    def test_restart_preserves_failed_receipt_and_appends_retry_chain(self):
        with self.assertRaises(ValueError):
            IterationBrain(StagedModel("semantic_grading"), self.root / "analysis").analyze(**self.args())
        failed_receipts = sorted((self.root / "analysis").glob("agent-call-receipt-*.json"))
        self.assertEqual(1, len(failed_receipts))
        self.assertEqual("failed", json.loads(failed_receipts[0].read_text())["status"])

        decision = IterationBrain(StagedModel(), self.root / "analysis").analyze(**self.args())
        receipts = sorted((self.root / "analysis").glob("agent-call-receipt-*.json"))
        self.assertEqual(4, len(receipts))
        self.assertEqual("failed", json.loads(receipts[0].read_text())["status"])
        self.assertEqual(3, sum(json.loads(path.read_text())["status"] == "succeeded" for path in receipts))
        self.assertEqual(receipts, [Path(path) for path in decision["analysis_artifacts"]["agent_call_receipt_history"]])

    def test_query_escape_and_limit_are_bounded(self):
        port = LocalEvidenceQueryPort(self.root, max_bytes=4, max_queries=1)
        with self.assertRaises(EvidenceQueryError):
            port.read_log_window(str(self.root.parent / "outside"), 0, 1)
        with self.assertRaises(EvidenceQueryError):
            port.read_log_window(str(self.run), 0, 1)

    def test_hard_exact_fact_cannot_be_reversed(self):
        args = self.args(cases=({"id": "c", "prompt": "judge", "expected_output": "expected"},))
        decision = IterationBrain(StagedModel(), self.root / "analysis").analyze(**args)
        self.assertEqual("fail", decision["case_results"][0]["status"])
        self.assertEqual(["c"], decision["failed_case_ids"])


if __name__ == "__main__":
    unittest.main()
