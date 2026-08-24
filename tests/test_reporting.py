import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aceval.contracts import (
    GradeResult,
    GradeStatus,
    MetricSourceSpec,
    ObjectiveSpec,
    RunObservation,
    TraceEvent,
)
from aceval.failure_attribution import FailureAttributor
from aceval.objectives import compare_objective
from aceval.orchestrator import (
    CandidateTrial,
    ComparisonResult,
    EvalRun,
    OptimizationResult,
    ScenarioEvaluation,
)
from aceval.reporting import to_report_dict, write_report


def make_run(
    root: Path,
    variant: str,
    dev_status: GradeStatus,
    validation_status: GradeStatus = GradeStatus.PASS,
    runtime_id: str = "reference",
    usage=None,
    subject_uri=None,
) -> EvalRun:
    scenarios = []
    for scenario_id, split, status in (
        ("dev-case", "dev", dev_status),
        ("validation-case", "validation", validation_status),
    ):
        scenarios.append(
            ScenarioEvaluation(
                scenario_id=scenario_id,
                split=split,
                status=status,
                grades=(
                    GradeResult(
                        grader_id="artifact",
                        status=status,
                        evidence=({"artifact": b"\x00\xff"},),
                    ),
                ),
                observation=RunObservation(
                    output={"ok": status == GradeStatus.PASS},
                    artifacts={"result.bin": b"\x00\xff"},
                    usage=dict(usage or {}),
                ),
            )
        )
    return EvalRun(
        run_id="run-" + variant,
        pack_name="csv-summary-smoke",
        pack_hash="pack-hash",
        suite_hash="suite-hash",
        subject_hash="subject-" + variant,
        subject_uri=subject_uri or str(root / variant),
        subject_variant=variant,
        runtime_id=runtime_id,
        requested_splits=("dev", "validation"),
        scenarios=tuple(scenarios),
        duration_seconds=1.25,
        run_dir=root / ("run-" + variant),
        limitations=("Reference-runtime scores are not cross-platform guarantees.",),
    )


class ReportingTests(unittest.TestCase):
    def test_eval_run_report_is_json_safe_and_deterministic(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = make_run(root, "baseline", GradeStatus.FAIL)

            report = to_report_dict(run)
            encoded = json.dumps(report, allow_nan=False, sort_keys=True)
            detailed = to_report_dict(run, detailed=True)
            artifact = detailed["scenarios"][0]["observation"]["artifacts"]["result.bin"]

            self.assertEqual(report["report_type"], "eval_run")
            self.assertEqual(report["detail_level"], "summary")
            self.assertEqual(report["run_dir"], str(run.run_dir))
            self.assertEqual(report["scenarios"][0]["status"], "fail")
            self.assertIs(type(report["scenarios"][0]["status"]), str)
            self.assertNotIn("observation", report["scenarios"][0])
            self.assertNotIn("evidence", report["scenarios"][0]["grades"][0])
            self.assertEqual(report["scenarios"][0]["grades"][0]["evidence_count"], 1)
            self.assertEqual(detailed["detail_level"], "detailed")
            self.assertEqual(artifact["$type"], "bytes")
            self.assertEqual(artifact["preview"], "00ff")
            self.assertTrue(artifact["content_omitted"])
            self.assertNotIn("data_base64", artifact)
            self.assertIsNone(report["summary"]["usage"])
            self.assertIsNone(report["summary"]["total_tokens"])
            self.assertIsNone(report["summary"]["cost_usd"])
            self.assertIsNone(report["summary"]["flake_rate"])
            self.assertEqual(
                report["summary"]["measurement_status"]["flake_rate"],
                "not_measured",
            )
            self.assertIn('"schema_version": "aceval.report/v1"', encoded)
            self.assertNotIn("data_base64", json.dumps(detailed, sort_keys=True))

            first_json, first_markdown = write_report(run, root / "reports")
            first_json_text = first_json.read_text(encoding="utf-8")
            first_markdown_text = first_markdown.read_text(encoding="utf-8")
            write_report(run, root / "reports")

            self.assertEqual(first_json_text, first_json.read_text(encoding="utf-8"))
            self.assertEqual(first_markdown_text, first_markdown.read_text(encoding="utf-8"))
            self.assertNotIn(
                "observation", json.loads(first_json_text)["scenarios"][0]
            )
            self.assertIn("| Pack | csv-summary-smoke |", first_markdown_text)
            self.assertIn("| dev | 0/1 | 0/1 | 0.0% | FAIL |", first_markdown_text)
            self.assertIn("## Limitations", first_markdown_text)

    def test_run_report_counts_and_renders_failure_attribution(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = make_run(root, "baseline", GradeStatus.FAIL)
            attributor = FailureAttributor()
            allowed = attributor.attribute(
                "dev-case",
                observation=run.scenarios[0].observation,
                grades=run.scenarios[0].grades,
            )
            blocked = attributor.attribute(
                "validation-case",
                observation=RunObservation(
                    trace=(
                        TraceEvent(
                            kind="tool_result",
                            seq=7,
                            tool="process_exec",
                            payload={
                                "ok": False,
                                "exit_code": 127,
                                "stderr_excerpt": "command not found",
                            },
                        ),
                    )
                ),
            )
            run = replace(
                run,
                scenarios=(
                    replace(run.scenarios[0], diagnostic_report=allowed),
                    replace(run.scenarios[1], diagnostic_report=blocked),
                ),
            )

            report = to_report_dict(run)
            _, markdown_path = write_report(run, root / "diagnostic-report")
            diagnostics = report["summary"]["diagnostics"]
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertEqual(
                {
                    "allow_skill_intervention": 1,
                    "deny_skill_intervention": 1,
                },
                diagnostics["scenario_decisions"],
            )
            self.assertEqual(2, diagnostics["failure_card_count"])
            self.assertEqual(1, diagnostics["patchable_card_count"])
            self.assertEqual(1, diagnostics["blocked_card_count"])
            self.assertEqual(
                {"quality_failure": 1, "tool_or_dependency": 1},
                diagnostics["categories"],
            )
            self.assertEqual(
                {"agent_behavior": 1, "cli": 1},
                diagnostics["observed_components"],
            )
            self.assertIn("## Failure Attribution", markdown)
            self.assertIn(
                "| dev-case | allow_skill_intervention | 1 | — |",
                markdown,
            )
            self.assertIn(
                "| validation-case | deny_skill_intervention | 1 | cli.binary_not_found |",
                markdown,
            )

    def test_usage_simulation_flag_and_html_escaping_are_explicit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = make_run(
                root,
                "fake-candidate",
                GradeStatus.PASS,
                runtime_id="fake",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd": 0.01,
                },
                subject_uri="<script>alert('x')</script>|subject",
            )

            report = to_report_dict(run)
            _, markdown_path = write_report(run, root / "fake-report")
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertTrue(report["simulated"])
            self.assertTrue(report["summary"]["simulated"])
            self.assertEqual(report["summary"]["input_tokens"], 20)
            self.assertEqual(report["summary"]["output_tokens"], 10)
            self.assertEqual(report["summary"]["total_tokens"], 30)
            self.assertAlmostEqual(report["summary"]["cost_usd"], 0.02)
            self.assertEqual(
                report["summary"]["measurement_status"]["tokens"], "measured"
            )
            self.assertIn("SIMULATED RESULT", markdown)
            self.assertNotIn("<script>", markdown)
            self.assertIn(
                "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;\\|subject",
                markdown,
            )

    def test_custom_simulation_flag_and_partial_measurements_are_preserved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = make_run(
                root,
                "custom-sim",
                GradeStatus.PASS,
                runtime_id="custom-sim",
                usage={"total_tokens": 5},
            )
            second = run.scenarios[1]
            run = replace(
                run,
                simulated=True,
                scenarios=(
                    run.scenarios[0],
                    replace(
                        second,
                        observation=replace(second.observation, usage={}),
                    ),
                ),
            )

            report = to_report_dict(run)

            self.assertTrue(report["simulated"])
            self.assertTrue(report["summary"]["simulated"])
            self.assertEqual(
                "partial", report["summary"]["measurement_status"]["tokens"]
            )
            self.assertEqual(
                {"usage": 1, "tokens": 1, "cost": 0, "total": 2},
                report["summary"]["measured_scenarios"],
            )

    def test_gate_summary_distinguishes_infrastructure_outcomes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = make_run(root, "baseline", GradeStatus.FAIL)
            validation_error = make_run(
                root,
                "validation-error",
                GradeStatus.FAIL,
                validation_status=GradeStatus.ERROR,
            )
            result = OptimizationResult(
                optimization_id="infra-stop",
                pack_name="csv-summary-smoke",
                baseline_dev=baseline,
                baseline_validation=validation_error,
                candidate_holdout=validation_error,
                holdout_batch_count=1,
                accepted=False,
                stop_reason="infrastructure failure",
            )

            summary = to_report_dict(result)["summary"]
            validation_gate = next(
                gate for gate in summary["gates"] if gate["gate"] == "validation"
            )

            self.assertEqual("error", validation_gate["status"])
            self.assertEqual("not_run", validation_gate["candidate_status"])
            self.assertEqual(
                "attempted_not_evaluable",
                summary["measurement_status"]["candidate_holdout"],
            )

    def test_comparison_report_shows_paired_uplift_and_regressions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = make_run(root, "baseline", GradeStatus.FAIL)
            candidate = make_run(
                root,
                "candidate",
                GradeStatus.PASS,
                validation_status=GradeStatus.FAIL,
            )
            comparison = ComparisonResult(
                baseline=baseline,
                candidate=candidate,
                paired_uplift=0.0,
                improvements=("dev-case",),
                hard_regressions=("validation-case",),
            )

            report = to_report_dict(comparison)
            _, markdown_path = write_report(comparison, root / "comparison")
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertFalse(report["accepted"])
            self.assertEqual(report["hard_regression_count"], 1)
            self.assertIn("| Paired uplift | +0.0 pp |", markdown)
            self.assertIn("| Hard regressions | validation-case |", markdown)
            self.assertIn("baseline →", markdown)

    def test_comparison_report_marks_infrastructure_failure_not_evaluable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = make_run(root, "baseline", GradeStatus.ERROR)
            candidate = make_run(root, "candidate", GradeStatus.PASS)
            comparison = ComparisonResult(
                baseline=baseline,
                candidate=candidate,
                paired_uplift=None,
                improvements=(),
                hard_regressions=(),
            )

            report = to_report_dict(comparison)
            _, markdown_path = write_report(comparison, root / "comparison-error")
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertFalse(report["evaluable"])
            self.assertEqual("error", report["status"])
            self.assertIsNone(report["summary"]["paired_uplift"])
            self.assertIn("| Decision | error |", markdown)
            self.assertIn("| Paired uplift | — |", markdown)

    def test_optimization_report_shows_gates_stop_reason_and_trials(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = make_run(root, "baseline", GradeStatus.FAIL)
            candidate = make_run(root, "candidate", GradeStatus.PASS)
            trial = CandidateTrial(
                round_index=1,
                candidate_id="candidate-1",
                candidate_path=root / "candidate-1",
                candidate_hash="candidate-hash",
                parent_hash="baseline-hash",
                patch="--- a/SKILL.md\n+++ b/SKILL.md\n",
                rationale="Address the failed output contract.",
                dev_run=candidate,
                promoted_from_dev=True,
            )
            optimization = OptimizationResult(
                optimization_id="optimization-1",
                pack_name="csv-summary-smoke",
                baseline_dev=baseline,
                trials=(trial,),
                baseline_validation=baseline,
                candidate_validation=candidate,
                selected_candidate_id="candidate-1",
                selected_candidate_path=root / "candidate-1",
                selected_candidate_hash="candidate-hash",
                accepted=True,
                stop_reason="candidate passed dev and validation",
                output_dir=root / "optimization-1",
                limitations=("Optimizer only received dev evidence.",),
                eval_suite_hash="suite-hash",
                experiment_plan_hash="experiment-hash",
                experiment_plan_source="external",
            )

            report = to_report_dict(optimization)
            _, markdown_path = write_report(
                optimization, root / "optimization-report", basename="result"
            )
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertTrue(report["summary"]["accepted"])
            self.assertEqual(report["summary"]["promoted_trial_count"], 1)
            self.assertEqual(report["summary"]["holdout_batch_count"], 0)
            self.assertEqual(report["summary"]["proposal_attempt_count"], 0)
            self.assertEqual(report["summary"]["experiment_usage"], {})
            self.assertEqual(report["summary"]["eval_suite_hash"], "suite-hash")
            self.assertEqual(
                report["summary"]["experiment_plan_hash"], "experiment-hash"
            )
            self.assertEqual(
                report["summary"]["experiment_plan_source"], "external"
            )
            self.assertEqual(
                report["summary"]["selected_candidate_hash"], "candidate-hash"
            )
            self.assertIsNone(report["summary"]["hidden_regression_rate"])
            self.assertEqual(
                report["summary"]["measurement_status"]["baseline_holdout"],
                "not_measured",
            )
            self.assertEqual(
                report["summary"]["measurement_status"]["candidate_holdout"],
                "not_run",
            )
            self.assertNotIn("patch", report["trials"][0])
            self.assertTrue(report["trials"][0]["patch_omitted"])
            self.assertIn("## Candidate Gates", markdown)
            self.assertIn("| dev | pass |", markdown)
            self.assertIn("candidate passed dev and validation", markdown)
            self.assertIn("| ExperimentPlan source | external |", markdown)
            self.assertIn("| Hidden regression rate | not measured |", markdown)
            self.assertIn("Optimizer only received dev evidence.", markdown)

    def test_tune_report_exposes_objective_and_paired_holdout(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = make_run(
                root,
                "baseline",
                GradeStatus.PASS,
                usage={"total_tokens": 100},
            )
            candidate = make_run(
                root,
                "candidate",
                GradeStatus.PASS,
                usage={"total_tokens": 50},
            )
            objective = ObjectiveSpec(
                id="token-efficiency",
                source=MetricSourceSpec(type="usage", key="total_tokens"),
                direction="minimize",
                min_delta=10,
            )
            objective_comparison = compare_objective(
                baseline, candidate, objective
            )
            trial = CandidateTrial(
                round_index=1,
                candidate_id="candidate-1",
                candidate_path=root / "candidate-1",
                candidate_hash="candidate-hash",
                parent_hash="baseline-hash",
                patch="--- a/SKILL.md\n+++ b/SKILL.md\n",
                rationale="Reduce redundant steps.",
                dev_run=candidate,
                promoted_from_dev=True,
                objective_comparison=objective_comparison,
            )
            optimization = OptimizationResult(
                optimization_id="tune-1",
                pack_name="csv-summary-smoke",
                baseline_dev=baseline,
                trials=(trial,),
                baseline_validation=baseline,
                candidate_validation=candidate,
                baseline_holdout=baseline,
                candidate_holdout=candidate,
                holdout_batch_count=1,
                holdout_pair_count=1,
                selected_candidate_id="candidate-1",
                selected_candidate_path=root / "candidate-1",
                selected_candidate_hash="candidate-hash",
                accepted=True,
                stop_reason="candidate passed paired tune gates",
                mode="tune",
                goal="Reduce tokens without changing correctness.",
                objective=objective,
                dev_objective=objective_comparison,
                validation_objective=objective_comparison,
                holdout_objective=objective_comparison,
            )

            report = to_report_dict(optimization)
            _, markdown_path = write_report(
                optimization, root / "tune-report"
            )
            summary = report["summary"]
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertEqual("tune", summary["mode"])
            self.assertEqual("token-efficiency", summary["objective"]["id"])
            self.assertTrue(summary["dev_objective"]["passed"])
            self.assertEqual(50.0, summary["holdout_objective"]["improvement"])
            self.assertEqual(1, summary["holdout_pair_count"])
            self.assertEqual(
                "measured",
                summary["measurement_status"]["baseline_holdout"],
            )
            holdout_gate = next(
                gate for gate in summary["gates"] if gate["gate"] == "holdout"
            )
            self.assertEqual("pass", holdout_gate["status"])
            self.assertTrue(holdout_gate["objective"]["passed"])
            self.assertIn("| Mode | tune |", markdown)
            self.assertIn("| Objective | token-efficiency |", markdown)
            self.assertIn("| Holdout pairs | 1 |", markdown)
            self.assertIn("| holdout | pass |", markdown)

    def test_write_report_rejects_path_like_basename(self):
        with TemporaryDirectory() as directory:
            run = make_run(Path(directory), "baseline", GradeStatus.FAIL)
            with self.assertRaises(ValueError):
                write_report(run, Path(directory), "nested/report")


if __name__ == "__main__":
    unittest.main()
