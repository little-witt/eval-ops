import json
import tempfile
import unittest
from pathlib import Path

from aceval.agent_runtime import ModelReply
from aceval.iteration_brain import EvidenceQueryError, IterationBrain, LocalEvidenceQueryPort, _json, _overall_assessment, _parse_semantic


class StagedModel:
    model_id = "forge-test-model"
    profile = "forge-test-profile"

    def __init__(self, invalid_stage=None, semantic_status="fail"):
        self.calls = []
        self.invalid_stage = invalid_stage
        self.semantic_status = semantic_status

    def complete(self, messages, tools):
        stage = next(name for name in ("semantic_grading", "attribution", "proposal") if name in messages[0]["content"])
        request = json.loads(messages[-1]["content"])
        self.calls.append((stage, request))
        if stage == self.invalid_stage:
            return ModelReply(content="not-json")
        if stage == "semantic_grading":
            return ModelReply(content=json.dumps({
                "case_results": [{"case_id": c["id"], "status": self.semantic_status, "verification_status": "not_run", "reason": "semantic defect", "evidence_refs": []} for c in request["cases"]],
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
        self.assertEqual("skill_improvement_candidate", decision["case_assessments"][0]["attribution"])
        self.assertIn("Skill", decision["case_assessments"][0]["conclusion"])

    def test_completed_trace_is_analyzable_without_an_oracle(self):
        decision = IterationBrain(None, self.root / "analysis").analyze(**self.args())
        # A complete session is itself an executable observation.  Lack of an
        # exact Oracle must not manufacture an ``evidence_gap`` or trigger a
        # remote replay; the result is held for semantic/stability analysis.
        self.assertEqual("pass_pending_verification", decision["case_assessments"][0]["attribution"])
        self.assertTrue(decision["case_assessments"][0]["reason"])
        self.assertTrue(decision["case_assessments"][0]["recommended_action"])
        self.assertEqual([], decision["not_evaluable_case_ids"])
        self.assertEqual([], decision["evidence_issues"])

    def test_complete_trace_downgrades_semantic_not_evaluable_to_execution_verdict(self):
        decision = IterationBrain(StagedModel(semantic_status="not_evaluable"), self.root / "analysis-semantic-gap").analyze(**self.args())
        self.assertEqual([], decision["not_evaluable_case_ids"])
        self.assertEqual("pass", decision["case_results"][0]["status"])
        self.assertEqual("pass", decision["case_assessments"][0]["display_status"])

    def test_pending_stability_is_not_reported_as_skill_failure(self):
        decision = IterationBrain(None, self.root / "analysis").analyze(**self.args(cases=({"id": "c", "prompt": "judge", "expected_output": "observed"},)))
        assessment = decision["case_assessments"][0]
        self.assertEqual("pass", assessment["display_status"])
        self.assertEqual("pending", assessment["stability_status"])
        self.assertEqual("pass_pending_verification", assessment["attribution"])
        self.assertIn("稳定性复验", assessment["conclusion"])
        self.assertIn("不要修改 Skill", assessment["recommended_action"])
        self.assertIn("测试分支", assessment["fixture_recommendation"])

    def test_mixed_pass_and_fail_waits_for_repair_before_stability(self):
        class MixedModel(StagedModel):
            def complete(self, messages, tools):
                stage = next(name for name in ("semantic_grading", "attribution", "proposal") if name in messages[0]["content"])
                request = json.loads(messages[-1]["content"])
                self.calls.append((stage, request))
                if stage == "semantic_grading":
                    return ModelReply(content=json.dumps({
                        "case_results": [
                            {"case_id": c["id"], "status": "fail" if c["id"] == "bad" else "pass", "verification_status": "not_run", "reason": "mixed result", "evidence_refs": []}
                            for c in request["cases"]
                        ],
                        "without_skill_baseline_case_results": [],
                    }))
                if stage == "attribution":
                    return ModelReply(content=json.dumps({"failure_clusters": [{"id": "bad-cluster", "case_ids": ["bad"], "root_cause": "defect", "skill_change_authorized": True}], "conflicts": []}))
                return ModelReply(content=json.dumps({"proposed_changes": [{"target": "SKILL.md", "change": "fix defect", "why": "evidence", "case_ids": ["bad"]}], "target_scope": ["SKILL.md"]}))

        decision = IterationBrain(MixedModel(), self.root / "analysis-mixed").analyze(**self.args(
            cases=(
                {"id": "good", "prompt": "judge good"},
                {"id": "bad", "prompt": "judge bad"},
            ),
            primary_batch={"cases": [
                {"case_id": "good", "status": "completed", "artifact": str(self.run)},
                {"case_id": "bad", "status": "completed", "artifact": str(self.run)},
            ]},
        ))
        self.assertIn(decision["next_action"], {"await_user_confirmation", "needs_evidence"})
        self.assertNotEqual("verify_passes", decision["next_action"])
        self.assertEqual(["bad"], decision["failed_case_ids"])
        self.assertEqual(["good"], decision["verification_required_case_ids"])

    def test_case_goal_observations_and_overall_optimization_summary(self):
        case = {
            "id": "review",
            "title": "复杂度审查",
            "prompt": "检查变更并输出审查报告",
            "expected_output": "expected",
            "metadata": {
                "aceval_test": {
                    "title": "复杂度审查",
                    "generation_reason": "验证 Skill 的代码审查能力",
                    "oracle_ready": True,
                    "oracle_trust": "human_confirmed",
                    "expected_observables": [
                        "读取 git diff",
                        "输出结构化审查报告",
                        "执行 analyze_complexity.js",
                    ],
                }
            },
        }
        self.run.write_text(json.dumps({"session": {"completeness": {"trace": True, "output": True}, "observation": {"output": "读取 git diff 后输出结构化审查报告", "trace": [{"kind": "tool_call", "tool": "shell", "command": "git diff"}], "metadata": {}, "error": None}}}), encoding="utf-8")
        model = StagedModel()
        decision = IterationBrain(model, self.root / "analysis").analyze(**self.args(
            cases=(case,),
            primary_batch={"cases": [{"case_id": "review", "status": "completed", "artifact": str(self.run)}]},
        ))
        observations = decision["case_assessments"][0]["goal_observations"]
        self.assertTrue(observations["trace_analyzed"])
        statuses = {item["requirement"]: item["status"] for item in observations["items"]}
        self.assertEqual("observed", statuses["读取 git diff"])
        self.assertEqual("observed", statuses["输出结构化审查报告"])
        self.assertEqual("not_observed", statuses["执行 analyze_complexity.js"])
        overall = decision["overall_assessment"]
        self.assertTrue(overall["skill_optimization_plan"])
        self.assertEqual("optimize_skill_then_verify", overall["next_action"])
        self.assertIn("同一批冻结 Case", overall["next_step"])
        self.assertEqual(["SKILL.md"], decision["target_scope"])
        self.assertEqual("goal", model.calls[0][1]["evaluation_goal"])
        self.assertEqual(["correct"], model.calls[0][1]["evaluation_standards"])
        verdict = overall["user_verdict"]
        self.assertEqual("needs_improvement", verdict["status"])
        self.assertTrue(verdict["problem"])
        self.assertTrue(verdict["evidence"])
        self.assertTrue(verdict["fix"])

    def test_oversized_stage_context_is_compacted_before_model_call(self):
        model = StagedModel()
        args = self.args(
            cases=({"id": "c", "prompt": "judge " + "x" * 6000},),
            primary_batch={"cases": [{"case_id": "c", "status": "completed", "artifact": str(self.run)}]},
        )
        decision = IterationBrain(model, self.root / "analysis", max_prompt_chars=2200).analyze(**args)
        self.assertTrue(model.calls)
        self.assertLessEqual(len(json.dumps(model.calls[0][1], ensure_ascii=False)), 2200)
        self.assertIn("case_aggregates", decision)

    def test_semantic_grading_is_partitioned_without_dropping_cases(self):
        model = StagedModel()
        cases = tuple({"id": "c%d" % index, "prompt": "judge " + "x" * 5000} for index in range(3))
        primary = {"cases": [{"case_id": case["id"], "status": "completed", "artifact": str(self.run)} for case in cases]}
        decision = IterationBrain(model, self.root / "analysis", max_prompt_chars=3000).analyze(
            **self.args(cases=cases, primary_batch=primary)
        )
        semantic_calls = [request for stage, request in model.calls if stage == "semantic_grading"]
        self.assertGreaterEqual(len(semantic_calls), 2)
        self.assertEqual({case["id"] for case in cases}, {item["case_id"] for item in decision["case_results"]})
        self.assertTrue(all(len(json.dumps(request, ensure_ascii=False)) <= 3000 for request in semantic_calls))

    def test_json_parser_accepts_claude_markdown_wrapper_and_preface(self):
        self.assertEqual({"ok": True}, _json("Here is the result:\n```json\n{\"ok\": true}\n```\n", "semantic_grading"))
        self.assertEqual({"ok": True}, _json("Result: {\"ok\": true} done", "semantic_grading"))
        self.assertEqual({"case_results": [{"case_id": "c1", "status": "pass"}]}, _json('[{"case_id":"c1","status":"pass"}]', "semantic_grading"))

    def test_semantic_parser_keeps_additive_dimension_extensions(self):
        parsed = _parse_semantic({
            "case_results": [{"case_id": "c", "status": "pass", "reason": "ok", "evidence_refs": []}],
            "dimensions": [{"id": "skill_execution", "score": 0.9}],
            "confidence": 0.8,
        }, ["c"], [])
        self.assertEqual("skill_execution", parsed["model_extensions"]["dimensions"][0]["id"])
        self.assertEqual(0.8, parsed["model_extensions"]["confidence"])

    def test_semantic_parser_normalizes_case_map_and_wrapped_envelope(self):
        parsed = _parse_semantic({
            "semantic_verdict": {
                "results": {
                    "c": {"status": "not_evaluable", "reason": "missing trace", "evidence_refs": []}
                }
            }
        }, ["c"], [])
        self.assertEqual("c", parsed["case_results"][0]["case_id"])
        self.assertEqual("not_evaluable", parsed["case_results"][0]["status"])

    def test_semantic_parser_accepts_single_case_row_and_status_shorthand(self):
        parsed = _parse_semantic(
            {"case_id": "c", "status": "通过", "reason": "ok", "evidence_refs": []},
            ["c"],
            [],
        )
        self.assertEqual("c", parsed["case_results"][0]["case_id"])
        self.assertEqual("pass", parsed["case_results"][0]["status"])
        parsed = _parse_semantic(
            {"semantic_verdict": {"status": "fail", "reason": "missing evidence", "evidence_refs": []}},
            ["c"],
            [],
        )
        self.assertEqual("c", parsed["case_results"][0]["case_id"])
        self.assertEqual("fail", parsed["case_results"][0]["status"])
        parsed = _parse_semantic({"case_results": "not_evaluable"}, ["c"], [])
        self.assertEqual("not_evaluable", parsed["case_results"][0]["status"])

    def test_semantic_parser_accepts_localized_and_qualified_statuses(self):
        parsed = _parse_semantic({
            "case_results": [
                {"case_id": "a", "status": "未通过（输出缺失）", "reason": "x", "evidence_refs": []},
                {"case_id": "b", "verdict": "not evaluated", "reason": "y", "evidence_refs": []},
            ]
        }, ["a", "b"], [])
        self.assertEqual(["fail", "not_evaluable"], [item["status"] for item in parsed["case_results"]])

    def test_semantic_parser_allows_baseline_only_chunk_without_primary_rows(self):
        parsed = _parse_semantic(
            {"without_skill_baseline_case_results": [{"case_id": "base", "status": "fail", "reason": "x", "evidence_refs": []}]},
            [],
            ["base"],
        )
        self.assertEqual([], parsed["case_results"])
        self.assertEqual("fail", parsed["without_skill_baseline_case_results"][0]["status"])

    def test_semantic_parser_conservatively_handles_null_single_case_results(self):
        parsed = _parse_semantic({"case_results": None}, ["c"], [])
        self.assertEqual("not_evaluable", parsed["case_results"][0]["status"])

    def test_semantic_parser_downgrades_unshaped_case_results_instead_of_blocking(self):
        parsed = _parse_semantic({"case_results": ["fail", "pass"]}, ["a", "b"], [])
        self.assertEqual(["fail", "pass"], [item["status"] for item in parsed["case_results"]])
        parsed = _parse_semantic({"case_results": {"unexpected": ["not-a-row"]}}, ["c"], [])
        self.assertEqual("not_evaluable", parsed["case_results"][0]["status"])

    def test_semantic_parser_downgrades_unknown_status_without_blocking_confirmation(self):
        parsed = _parse_semantic({
            "case_results": [{"case_id": "c", "status": "maybe", "reason": "ambiguous", "evidence_refs": []}],
        }, ["c"], [])
        row = parsed["case_results"][0]
        self.assertEqual("not_evaluable", row["status"])
        self.assertEqual("maybe", row["status_raw"])
        self.assertIn("未识别状态", row["reason"])

    def test_failed_dimensions_expose_output_trace_facts_in_decision_and_graph(self):
        case = {"id": "c", "prompt": "judge", "expected_output": "expected"}
        decision = IterationBrain(None, self.root / "analysis").analyze(**self.args(cases=(case,)))
        assessment = decision["case_assessments"][0]
        outcome = next(item for item in assessment["dimensions"] if item["dimension"] == "outcome")
        self.assertIn("期望", outcome["reason"])
        self.assertIn("实际输出", outcome["reason"])
        self.assertTrue(outcome["evidence_detail"])
        fact = decision["diagnosis_graph"]["clusters"][0]["facts"][0]
        self.assertTrue(fact["failure_dimensions"])
        self.assertIn("结果是否符合 Case 目标", fact["reason"])
        matrix = decision["overall_assessment"]["causal_matrix"][0]
        self.assertTrue(matrix["failure_dimensions"])
        self.assertTrue(matrix["skill_factors"])

    def test_open_ended_output_basis_does_not_call_missing_expectation_an_expected_value(self):
        decision = IterationBrain(None, self.root / "analysis-open-ended").analyze(**self.args())
        outcome = next(item for item in decision["case_assessments"][0]["dimensions"] if item["dimension"] == "outcome")
        self.assertIn("未声明精确期望", outcome["evidence_detail"])
        self.assertNotIn("期望「未声明精确期望」", outcome["evidence_detail"])

    def test_overall_summary_does_not_copy_long_agent_report(self):
        assessment = {
            "case_id": "c",
            "status": "fail",
            "attribution": "skill_improvement_candidate",
            "dimension_summaries": [{
                "dimension": "outcome",
                "label": "结果是否符合 Case 目标",
                "status": "fail",
                "reason": "模型给出完整报告：执行过程总结 | 步骤 | 结果 |",
                "evidence_detail": "未声明精确期望；实际输出「以上即为完整的 PR 审查报告……执行过程总结……」",
                "evidence_refs": [],
            }],
            "goal_observations": {"missing_requirements": []},
        }
        overall = _overall_assessment(
            [{"id": "c"}],
            [assessment],
            [{"case_ids": ["c"], "skill_change_authorized": True, "root_cause": "缺少目标核对规则"}],
            [{"target": "SKILL.md", "change": "在输出前增加目标核对清单", "why": "结果未满足目标", "case_ids": ["c"]}],
            [],
            "await_user_confirmation",
        )
        verdict = overall["user_verdict"]
        self.assertNotIn("执行过程总结", verdict["evidence"])
        self.assertNotIn("以上即为完整", verdict["evidence"])
        self.assertIn("Case 目标", verdict["evidence"])

    def test_overall_uses_compact_model_diagnosis_without_case_concatenation(self):
        assessment = {
            "case_id": "script",
            "status": "fail",
            "attribution": "skill_optimization_candidate",
            "dimension_summaries": [],
            "goal_observations": {"missing_requirements": []},
        }
        overall = _overall_assessment(
            [{"id": "script"}],
            [assessment],
            [{
                "case_ids": ["script"],
                "skill_change_authorized": True,
                "optimization_kind": "resilience",
                "problem_summary": "Skill 缺少完成前证据门禁。",
                "evidence_summary": "Trace 未出现脚本调用，但输出声称使用了脚本结果。",
            }],
            [{
                "target": "SKILL.md",
                "change": "在最终输出前校验脚本调用和成功结果。",
                "why": "阻止跳步提交",
                "case_ids": ["script"],
            }],
            [],
            "await_user_confirmation",
            model_summary={
                "problem": "关键步骤缺少可观察的完成门禁。",
                "evidence": "Agent 未运行脚本却引用了脚本结果。",
                "recommendation": "在最终输出前强制校验脚本调用及成功回执。",
            },
        )
        verdict = overall["user_verdict"]
        self.assertEqual("can_optimize", verdict["status"])
        self.assertEqual("关键步骤缺少可观察的完成门禁。", verdict["problem"])
        self.assertNotIn("Case", verdict["summary"])

    def test_overall_never_calls_all_pass_when_an_unauthorized_case_failed(self):
        assessments = [
            {
                "case_id": "failed",
                "status": "fail",
                "attribution": "failure_not_authorized",
                "reason": "评测口径仍需校准",
                "dimension_summaries": [],
                "goal_observations": {"missing_requirements": []},
            },
            {
                "case_id": "pending",
                "status": "pass",
                "attribution": "pass_pending_verification",
                "pending_verification": True,
                "dimension_summaries": [],
                "goal_observations": {"missing_requirements": []},
            },
        ]
        overall = _overall_assessment(
            [{"id": "failed"}, {"id": "pending"}],
            assessments,
            [],
            [],
            [],
            "needs_evidence",
        )
        self.assertNotEqual("verification_pending", overall["user_verdict"]["status"])
        self.assertIn("未通过", overall["conclusion"])
        self.assertNotEqual("verify_passes", overall["next_action"])


if __name__ == "__main__":
    unittest.main()
