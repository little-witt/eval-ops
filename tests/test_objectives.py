import unittest
from pathlib import Path

from aceval.contracts import (
    GradeResult,
    GradeStatus,
    MetricDirection,
    MetricSourceSpec,
    ObjectiveSpec,
    RunObservation,
    TraceEvent,
)
from aceval.objectives import compare_objective, measure_objective
from aceval.orchestrator import EvalRun, ScenarioEvaluation


def make_run(*scenarios):
    return EvalRun(
        run_id="objective-run",
        pack_name="objective-pack",
        pack_hash="pack-hash",
        suite_hash="suite-hash",
        subject_hash="subject-hash",
        subject_uri="subject",
        subject_variant="candidate",
        runtime_id="fake",
        requested_splits=("dev",),
        scenarios=tuple(scenarios),
        duration_seconds=sum(item.duration_seconds for item in scenarios),
        run_dir=Path("/tmp/objective-run"),
    )


def scenario(
    scenario_id,
    *,
    usage=None,
    score=None,
    metric=None,
    grade_status=GradeStatus.PASS,
    trace=(),
    duration=1.0,
):
    return ScenarioEvaluation(
        scenario_id=scenario_id,
        split="dev",
        status=grade_status,
        grades=(
            GradeResult(
                grader_id="quality",
                status=grade_status,
                score=score,
                metrics={} if metric is None else {"quality": metric},
            ),
        ),
        observation=RunObservation(usage=usage or {}, trace=trace),
        duration_seconds=duration,
    )


class ObjectiveTests(unittest.TestCase):
    def test_total_tokens_uses_the_largest_complete_accounting_view(self):
        run = make_run(
            scenario(
                "case-a",
                usage={
                    "total_tokens": 90,
                    "input_tokens": 60,
                    "output_tokens": 40,
                },
            ),
            scenario(
                "case-b",
                usage={"prompt_tokens": 20, "completion_tokens": 30},
            ),
        )
        objective = ObjectiveSpec(
            id="token-efficiency",
            source=MetricSourceSpec(type="usage", key="total_tokens"),
            direction=MetricDirection.MINIMIZE,
        )

        measurement = measure_objective(run, objective)

        self.assertEqual("measured", measurement.status)
        self.assertEqual(1.0, measurement.coverage)
        self.assertEqual(
            {"case-a": 100.0, "case-b": 50.0},
            dict(measurement.scenario_values),
        )
        self.assertEqual(75.0, measurement.value)

    def test_missing_or_invalid_usage_fails_closed(self):
        objective = ObjectiveSpec(
            id="token-efficiency",
            source=MetricSourceSpec(type="usage", key="total_tokens"),
            direction="minimize",
        )
        baseline = make_run(
            scenario("case-a", usage={"total_tokens": 100}),
            scenario("case-b", usage={"total_tokens": 100}),
        )
        candidate = make_run(
            scenario("case-a", usage={"total_tokens": 50}),
            scenario("case-b", usage={}),
        )
        invalid = make_run(
            scenario("case-a", usage={"total_tokens": -1}),
            scenario("case-b", usage={"total_tokens": float("nan")}),
        )

        partial = measure_objective(candidate, objective)
        comparison = compare_objective(baseline, candidate, objective)

        self.assertEqual("partial", partial.status)
        self.assertEqual(0.5, partial.coverage)
        self.assertFalse(comparison.evaluable)
        self.assertFalse(comparison.passed)
        self.assertIsNone(comparison.improvement)
        self.assertEqual("not_measured", measure_objective(invalid, objective).status)

    def test_grader_objective_ignores_error_and_not_evaluable_results(self):
        objective = ObjectiveSpec(
            id="quality",
            source=MetricSourceSpec(type="grader_score", grader_id="quality"),
        )
        run = make_run(
            scenario("pass", score=0.8),
            scenario("error", score=1.0, grade_status=GradeStatus.ERROR),
            scenario(
                "missing",
                score=1.0,
                grade_status=GradeStatus.NOT_EVALUABLE,
            ),
        )

        measurement = measure_objective(run, objective)

        self.assertEqual("partial", measurement.status)
        self.assertAlmostEqual(1 / 3, measurement.coverage)
        self.assertEqual({"pass": 0.8}, dict(measurement.scenario_values))

    def test_maximize_and_minimize_are_normalized_to_positive_improvement(self):
        baseline_quality = make_run(
            scenario("case-a", score=0.6), scenario("case-b", score=0.8)
        )
        candidate_quality = make_run(
            scenario("case-a", score=0.8), scenario("case-b", score=0.9)
        )
        quality = ObjectiveSpec(
            id="quality",
            source=MetricSourceSpec(type="grader_score", grader_id="quality"),
            direction="maximize",
            min_delta=0.1,
            target=0.84,
        )
        baseline_tokens = make_run(
            scenario("case-a", usage={"total_tokens": 100}),
            scenario("case-b", usage={"total_tokens": 100}),
        )
        candidate_tokens = make_run(
            scenario("case-a", usage={"total_tokens": 60}),
            scenario("case-b", usage={"total_tokens": 80}),
        )
        tokens = ObjectiveSpec(
            id="tokens",
            source=MetricSourceSpec(type="usage", key="total_tokens"),
            direction="minimize",
            min_delta=20,
            target=75,
        )

        quality_comparison = compare_objective(
            baseline_quality, candidate_quality, quality
        )
        token_comparison = compare_objective(
            baseline_tokens, candidate_tokens, tokens
        )

        self.assertAlmostEqual(0.15, quality_comparison.improvement)
        self.assertTrue(quality_comparison.passed)
        self.assertEqual(-30.0, token_comparison.raw_delta)
        self.assertEqual(30.0, token_comparison.improvement)
        self.assertTrue(token_comparison.passed)

    def test_per_case_regression_rejects_aggregate_improvement(self):
        baseline = make_run(
            scenario("case-a", usage={"total_tokens": 100}),
            scenario("case-b", usage={"total_tokens": 100}),
        )
        candidate = make_run(
            scenario("case-a", usage={"total_tokens": 80}),
            scenario("case-b", usage={"total_tokens": 106}),
        )
        objective = ObjectiveSpec(
            id="tokens",
            source=MetricSourceSpec(type="usage", key="total_tokens"),
            direction="minimize",
            max_case_regression=5,
        )

        comparison = compare_objective(baseline, candidate, objective)

        self.assertEqual(7.0, comparison.improvement)
        self.assertEqual(("case-b",), comparison.case_regressions)
        self.assertFalse(comparison.passed)

    def test_unchanged_metric_is_not_an_optimization(self):
        baseline = make_run(scenario("case-a", usage={"total_tokens": 100}))
        candidate = make_run(scenario("case-a", usage={"total_tokens": 100}))
        objective = ObjectiveSpec(
            id="tokens",
            source=MetricSourceSpec(type="usage", key="total_tokens"),
            direction="minimize",
            min_delta=0,
        )

        comparison = compare_objective(baseline, candidate, objective)

        self.assertTrue(comparison.evaluable)
        self.assertEqual(0.0, comparison.improvement)
        self.assertFalse(comparison.meets_min_delta)
        self.assertFalse(comparison.passed)

    def test_trace_count_and_duration_sources_are_measurable(self):
        run = make_run(
            scenario(
                "case-a",
                trace=(
                    TraceEvent(kind="tool_call", name="read_file"),
                    TraceEvent(kind="event", name="answer"),
                ),
                duration=2.5,
            )
        )
        tool_calls = ObjectiveSpec(
            id="tool-calls",
            source=MetricSourceSpec(type="trace_count", key="tool_call"),
            direction="minimize",
        )
        duration = ObjectiveSpec(
            id="duration",
            source=MetricSourceSpec(type="scenario", key="duration_seconds"),
            direction="minimize",
        )

        self.assertEqual(1.0, measure_objective(run, tool_calls).value)
        self.assertEqual(2.5, measure_objective(run, duration).value)


if __name__ == "__main__":
    unittest.main()
