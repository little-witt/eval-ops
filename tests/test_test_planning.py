import json
import unittest

from aceval.skill_analysis import analyze_skill
from aceval.test_planning import (
    ORACLE_READY_LEVELS,
    PlannerConfig,
    TestPlan,
    TestPlanner,
    TestPlanningError,
    plan_tests,
)


SKILL = """# Data Publisher

## Validate dataset

Inputs: `input.csv`
Outputs: `validated.json`

1. Use `read_file` to inspect the input.
2. Use `write_file` to create `validated.json`.
3. If input is empty, return an error without writing an artifact.

Never accept malformed records.

## Publish dataset

Inputs: `validated.json`
Outputs: publication receipt

1. Run `curl` to publish the data.
2. When the service times out, retry once and report failure.

Never publish unvalidated data.
"""


REFERENCE_CAPABILITIES = {
    "fresh_session",
    "workspace_fixture",
    "artifact_output",
    "canonical_trace",
}
FULL_CAPABILITIES = REFERENCE_CAPABILITIES | {
    "process_exec",
    "network",
    "fault_injection",
    "stateful_replay",
}


class TestPlanningTest(unittest.TestCase):
    def setUp(self):
        self.graph = analyze_skill(SKILL)

    def test_maps_seed_cases_and_uses_bounded_deterministic_greedy_selection(self):
        seeds = [
            {
                "id": "validate-seed",
                "prompt": "Validate dataset from input.csv.",
                "expected_output": {"valid": True},
            }
        ]
        first = plan_tests(
            self.graph,
            seed_cases=seeds,
            goal="Prevent malformed publication and unsafe side effects.",
            runtime_capabilities=FULL_CAPABILITIES,
            max_generated_cases=2,
        )
        second = plan_tests(
            self.graph,
            seed_cases=seeds,
            goal="Prevent malformed publication and unsafe side effects.",
            runtime_capabilities=FULL_CAPABILITIES,
            max_generated_cases=2,
        )

        self.assertEqual(first, second)
        self.assertEqual(3, len(first.cases))
        self.assertEqual(2, len(first.decisions))
        seed = first.cases[0]
        self.assertEqual("seed", seed.origin)
        self.assertTrue(seed.requirement_ids)
        self.assertIn("seed_derived", ORACLE_READY_LEVELS)
        self.assertTrue(seed.oracle_ready)
        self.assertTrue(seed.executable)
        self.assertTrue(all(item.score > 0 for item in first.decisions))
        self.assertGreaterEqual(
            first.decisions[0].score,
            first.decisions[1].score,
        )
        self.assertLessEqual(
            len([case for case in first.cases if case.origin != "seed"]),
            2,
        )

    def test_runtime_router_keeps_gaps_and_does_not_generate_false_cases(self):
        plan = plan_tests(
            self.graph,
            seed_cases=(),
            goal="Prevent unsafe publication.",
            runtime_capabilities=REFERENCE_CAPABILITIES,
            max_generated_cases=20,
        )

        publish_id = next(
            item.id for item in self.graph.capabilities if item.name == "Publish dataset"
        )
        publish_requirements = {
            item.id
            for item in plan.requirements
            if item.capability_id == publish_id
        }
        self.assertTrue(publish_requirements)
        self.assertTrue(
            any(
                gap.requirement_id in publish_requirements
                and gap.capability == "process_exec"
                and gap.gap_type == "unsupported"
                for gap in plan.runtime_gaps
            )
        )
        self.assertFalse(
            any(
                case.origin != "seed"
                and publish_requirements.intersection(case.requirement_ids)
                for case in plan.cases
            )
        )
        critical_publish = {
            item.id
            for item in plan.requirements
            if item.capability_id == publish_id and item.priority == "critical"
        }
        self.assertTrue(critical_publish)
        self.assertTrue(
            any(
                blocker.requirement_id in critical_publish
                and blocker.code == "critical_requirement_uncovered"
                for blocker in plan.freeze_blockers
            )
        )

    def test_greedy_budget_prioritizes_risk_weight_over_case_count(self):
        baseline = plan_tests(
            self.graph,
            runtime_capabilities=FULL_CAPABILITIES,
            max_generated_cases=0,
        )
        risk_ids = {
            item.id for item in baseline.requirements if item.dimension == "risk"
        }
        self.assertTrue(risk_ids)

        selected = plan_tests(
            self.graph,
            runtime_capabilities=FULL_CAPABILITIES,
            max_generated_cases=1,
        )

        self.assertEqual(1, len(selected.decisions))
        self.assertTrue(
            risk_ids.intersection(selected.decisions[0].added_requirement_ids)
        )
        generated = next(case for case in selected.cases if case.origin != "seed")
        self.assertEqual("model_proposed", generated.oracle_level)
        self.assertFalse(generated.oracle_ready)
        self.assertTrue(selected.budget_excluded_requirement_ids)

    def test_generated_workspace_case_does_not_invent_a_deterministic_oracle(self):
        graph = analyze_skill(
            """# Export Skill

## Export report

Outputs: `report.json`

Use `write_file` to create `report.json`.
"""
        )
        plan = plan_tests(
            graph,
            runtime_capabilities=REFERENCE_CAPABILITIES,
            max_generated_cases=10,
        )

        happy_requirement = next(
            item for item in plan.requirements if item.dimension == "happy_path"
        )
        generated = next(
            case
            for case in plan.cases
            if happy_requirement.id in case.requirement_ids
        )
        self.assertEqual("workspace_state", happy_requirement.oracle_strategy)
        self.assertEqual("model_proposed", generated.oracle_level)
        self.assertFalse(generated.oracle_ready)
        self.assertTrue(generated.needs_user_input)

    def test_unknown_runtime_is_reported_as_unknown_not_assumed_supported(self):
        plan = plan_tests(
            self.graph,
            seed_cases=[
                {
                    "id": "publish-seed",
                    "prompt": "Publish dataset.",
                    "expected_observables": ["A receipt is returned."],
                }
            ],
            runtime_capabilities=None,
            max_generated_cases=4,
        )

        self.assertEqual("unknown", plan.runtime_profile_state)
        self.assertEqual("unsupported_runtime", plan.outcome)
        self.assertTrue(plan.runtime_gaps)
        self.assertEqual({"unknown"}, {item.gap_type for item in plan.runtime_gaps})
        self.assertFalse(any(case.executable for case in plan.cases))

    def test_observables_without_an_executable_assertion_stay_pending(self):
        plan = plan_tests(
            self.graph,
            seed_cases=[
                {
                    "id": "publish-observable",
                    "prompt": "Publish dataset.",
                    "family_id": "publish-family",
                    "expected_observables": ["A receipt is returned."],
                    "oracle_level": "human_confirmed",
                }
            ],
            runtime_capabilities=FULL_CAPABILITIES,
            max_generated_cases=0,
        )

        case = plan.cases[0]
        self.assertEqual("human_confirmed", case.oracle_level)
        self.assertEqual("publish-family", case.family)
        self.assertFalse(case.oracle_ready)
        self.assertTrue(case.needs_user_input)
        self.assertEqual("needs_user_input", plan.outcome)

    def test_seed_traceability_and_contract_round_trip_fail_closed(self):
        plan = TestPlanner(PlannerConfig(max_generated_cases=0)).plan(
            self.graph,
            seed_cases=[
                {
                    "id": "unmapped",
                    "prompt": "Unrelated weather question.",
                }
            ],
            runtime_capabilities=FULL_CAPABILITIES,
        )

        self.assertTrue(
            any(item.code == "seed_case_unmapped" for item in plan.freeze_blockers)
        )
        self.assertEqual(plan, TestPlan.from_json(plan.to_json()))
        self.assertEqual(plan.to_dict(), json.loads(plan.to_json()))

        invalid = plan.to_dict()
        invalid["api_version"] = "aceval.test-plan/v2"
        with self.assertRaisesRegex(TestPlanningError, "unsupported.*api_version"):
            TestPlan.from_dict(invalid)

        malformed = plan.to_dict()
        malformed["cases"] = "not-an-array"
        with self.assertRaisesRegex(TestPlanningError, "JSON array"):
            TestPlan.from_dict(malformed)

        with self.assertRaisesRegex(TestPlanningError, "unknown requirements"):
            plan_tests(
                self.graph,
                seed_cases=[
                    {
                        "id": "bad",
                        "prompt": "Bad mapping",
                        "requirement_ids": ["req.does-not-exist"],
                    }
                ],
                runtime_capabilities=FULL_CAPABILITIES,
            )


if __name__ == "__main__":
    unittest.main()
