import dataclasses
import unittest
from pathlib import Path

from aceval.contracts import (
    Artifact,
    CandidatePatch,
    GradeResult,
    GradeStatus,
    ImprovementMode,
    MetricDirection,
    MetricSourceSpec,
    ObjectiveSpec,
    OptimizerPolicySpec,
    PackIssue,
    PackReport,
    PreparedScenario,
    RunBudget,
    RunObservation,
    RuntimeResult,
    RuntimeCapabilities,
    RuntimeProfile,
    TraceEvent,
    as_primitive,
)


class ContractTests(unittest.TestCase):
    def test_runtime_capabilities_are_normalized_and_checked(self):
        capabilities = RuntimeCapabilities.from_values(
            ["fresh_session", "canonical_trace", "fresh_session"]
        )

        self.assertTrue(capabilities.supports("fresh_session"))
        self.assertEqual(
            capabilities.missing(["canonical_trace", "workspace_fixture"]),
            frozenset({"workspace_fixture"}),
        )
        profile = RuntimeProfile(adapter="reference", capabilities=["fresh_session"])
        self.assertIsInstance(profile.capabilities, RuntimeCapabilities)

    def test_runtime_profile_is_deeply_frozen_and_content_addressed(self):
        parameters = {"nested": {"steps": [1, 2]}}
        first = RuntimeProfile(
            adapter="reference",
            capabilities=["fresh_session"],
            parameters=parameters,
            metadata={"profile_complete": False},
        )
        second = RuntimeProfile(
            adapter="reference",
            metadata={"profile_complete": False},
            parameters={"nested": {"steps": [1, 2]}},
            capabilities=["fresh_session"],
        )
        changed = RuntimeProfile(
            adapter="reference",
            capabilities=["fresh_session"],
            parameters={"nested": {"steps": [1, 3]}},
            metadata={"profile_complete": False},
        )

        parameters["nested"]["steps"].append(3)

        self.assertEqual((1, 2), first.parameters["nested"]["steps"])
        self.assertEqual(first.profile_hash, second.profile_hash)
        self.assertNotEqual(first.profile_hash, changed.profile_hash)
        with self.assertRaises(TypeError):
            first.parameters["new"] = True
        with self.assertRaises(ValueError):
            RuntimeProfile(adapter="reference", profile_hash="0" * 64)

    def test_observation_and_grade_accept_serializable_inputs(self):
        event = TraceEvent(kind="tool_call", name="read_file")
        observation = RunObservation(output={"ok": True}, trace=[event])
        grade = GradeResult(
            grader_id="schema",
            status="pass",
            evidence=[{"path": "output"}],
        )

        self.assertEqual(event.type, "tool_call")
        self.assertEqual(observation.final_output, {"ok": True})
        self.assertEqual(observation.trace, (event,))
        self.assertEqual(grade.status, GradeStatus.PASS)
        self.assertTrue(grade.passed)
        self.assertEqual(as_primitive(grade)["status"], "pass")

    def test_runtime_and_grading_boundaries_are_deeply_frozen(self):
        baseline = {"input.txt": Artifact(path="input.txt", content=b"before")}
        prepared_metadata = {"nested": {"enabled": True}}
        prepared = PreparedScenario(
            baseline_files=baseline,
            metadata=prepared_metadata,
        )
        runtime_output = {"records": [{"id": 1}]}
        runtime = RuntimeResult(
            final_output=runtime_output,
            artifacts={"result.json": {"ok": True}},
            usage={"total_tokens": 1},
        )
        observation = RunObservation(
            output=runtime.final_output,
            artifacts=runtime.artifacts,
            pre_state=prepared.baseline_files,
            post_state=prepared.baseline_files,
        )
        grade = GradeResult(
            grader_id="immutable",
            status="pass",
            metrics={"nested": {"count": 1}},
            evidence=[{"path": ["records", 0]}],
        )

        baseline.clear()
        prepared_metadata["nested"]["enabled"] = False
        runtime_output["records"][0]["id"] = 999

        self.assertIn("input.txt", prepared.baseline_files)
        self.assertTrue(prepared.metadata["nested"]["enabled"])
        self.assertEqual(1, runtime.final_output["records"][0]["id"])
        self.assertEqual(1, observation.output["records"][0]["id"])
        with self.assertRaises(TypeError):
            observation.output["records"][0]["id"] = 2
        with self.assertRaises(TypeError):
            observation.pre_state["input.txt"] = None
        with self.assertRaises(TypeError):
            grade.metrics["nested"]["count"] = 2
        with self.assertRaises(TypeError):
            grade.evidence[0]["path"] = ()

    def test_contract_records_are_frozen(self):
        patch = CandidatePatch(base_hash="abc", unified_diff="", allowed_paths=["SKILL.md"])
        prepared = PreparedScenario(workspace="/tmp/example", artifact_paths=["result.json"])

        self.assertEqual(patch.allowed_paths, ("SKILL.md",))
        self.assertEqual(prepared.workspace, Path("/tmp/example"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            patch.base_hash = "changed"

    def test_pack_report_never_treats_an_error_as_ok(self):
        report = PackReport(issues=(PackIssue(code="bad", message="broken"),))

        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(len(report.warnings), 0)

    def test_run_budget_rejects_non_finite_and_wrong_typed_limits(self):
        invalid = (
            {"max_cost_usd": float("nan")},
            {"max_wall_time_seconds": float("inf")},
            {"max_total_tokens": 1.5},
            {"max_tool_calls": True},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                RunBudget(**values)

    def test_improvement_policy_and_objective_are_normalized_and_frozen(self):
        policy = OptimizerPolicySpec(
            adapter="skill_markdown_v1",
            mode="tune",
            goal="  reduce tokens  ",
            objective={
                "id": "token-efficiency",
                "source": {"type": "usage", "key": "total_tokens"},
                "direction": "minimize",
                "min_delta": 10,
            },
            params={"nested": {"enabled": True}},
        )

        self.assertEqual(ImprovementMode.TUNE, policy.mode)
        self.assertEqual("reduce tokens", policy.goal)
        self.assertEqual(MetricDirection.MINIMIZE, policy.objective.direction)
        self.assertEqual(10.0, policy.objective.min_delta)
        with self.assertRaises(TypeError):
            policy.params["nested"]["enabled"] = False

    def test_objective_rejects_invalid_numeric_and_aggregation_contracts(self):
        source = MetricSourceSpec(type="usage", key="total_tokens")
        invalid = (
            {"min_delta": -1},
            {"min_delta": float("nan")},
            {"max_case_regression": float("inf")},
            {"target": float("nan")},
            {"aggregation": "median"},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ObjectiveSpec(id="tokens", source=source, **values)

    def test_improvement_policy_rejects_mode_objective_mismatches(self):
        objective = ObjectiveSpec(
            id="tokens",
            source=MetricSourceSpec(type="usage", key="total_tokens"),
            direction="minimize",
        )

        with self.assertRaisesRegex(ValueError, "repair mode"):
            OptimizerPolicySpec(
                adapter="skill_markdown_v1",
                mode="repair",
                objective=objective,
            )
        with self.assertRaisesRegex(ValueError, "tune mode"):
            OptimizerPolicySpec(adapter="skill_markdown_v1", mode="tune")


if __name__ == "__main__":
    unittest.main()
