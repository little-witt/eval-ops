import json
import unittest

from aceval.coverage import (
    CoverageError,
    CoverageReport,
    build_coverage_report,
)
from aceval.skill_analysis import analyze_skill
from aceval.test_planning import plan_tests


SKILL = """# Mixed Runtime Skill

## Build local summary

Inputs: `input.csv`
Outputs: `summary.json`

1. Use `read_file` to read the input.
2. Use `write_file` to create `summary.json`.

## Upload summary

Inputs: `summary.json`
Outputs: upload receipt

1. Run `curl` to upload the summary.
2. If upload fails, return a structured error.
"""


REFERENCE_CAPABILITIES = {
    "fresh_session",
    "workspace_fixture",
    "artifact_output",
    "canonical_trace",
}


class CoverageTest(unittest.TestCase):
    def setUp(self):
        graph = analyze_skill(SKILL)
        discovery = plan_tests(
            graph,
            runtime_capabilities=REFERENCE_CAPABILITIES,
            max_generated_cases=0,
        )
        local_id = next(
            item.id
            for item in discovery.requirements
            if item.dimension == "happy_path"
            and "build-local-summary" in item.id
        )
        upload_id = next(
            item.id
            for item in discovery.requirements
            if item.dimension == "happy_path" and "upload-summary" in item.id
        )
        self.local_requirement_id = local_id
        self.upload_requirement_id = upload_id
        self.plan = plan_tests(
            graph,
            seed_cases=[
                {
                    "id": "local-case",
                    "prompt": "Build local summary.",
                    "requirement_ids": [local_id],
                    "expected_output": {"rows": 2},
                },
                {
                    "id": "upload-case",
                    "prompt": "Upload summary.",
                    "requirement_ids": [upload_id],
                    "expected_observables": ["An upload receipt is returned."],
                },
            ],
            runtime_capabilities=REFERENCE_CAPABILITIES,
            max_generated_cases=0,
        )

    def test_reports_planned_executable_oracle_ready_and_observed_separately(self):
        report = build_coverage_report(
            self.plan,
            observed_case_ids=["local-case"],
        )

        self.assertEqual(2, report.planned.covered)
        self.assertEqual(1, report.executable.covered)
        self.assertEqual(1, report.oracle_ready.covered)
        self.assertEqual(1, report.observed.covered)
        self.assertEqual(len(self.plan.requirements), report.planned.total)
        self.assertLess(report.planned.ratio, 1.0)
        self.assertLessEqual(report.executable.ratio, report.planned.ratio)
        local = next(
            item
            for item in report.requirements
            if item.requirement_id == self.local_requirement_id
        )
        upload = next(
            item
            for item in report.requirements
            if item.requirement_id == self.upload_requirement_id
        )
        self.assertTrue(local.planned)
        self.assertTrue(local.executable)
        self.assertTrue(local.oracle_ready)
        self.assertTrue(local.observed)
        self.assertTrue(upload.planned)
        self.assertFalse(upload.executable)
        self.assertTrue(
            any(reason.endswith(":process_exec") for reason in upload.gap_reasons)
        )

    def test_weighted_coverage_exposes_numerator_denominator_and_risk(self):
        report = build_coverage_report(self.plan)

        self.assertGreater(report.planned.total_weight, 0)
        self.assertGreater(report.planned.covered_weight, 0)
        self.assertAlmostEqual(
            report.planned.covered_weight / report.planned.total_weight,
            report.planned.weighted_ratio,
        )
        self.assertNotEqual(
            report.planned.ratio,
            report.planned.weighted_ratio,
        )
        uncovered = [item for item in report.requirements if not item.planned]
        self.assertTrue(uncovered)
        self.assertTrue(all("no_selected_case" in item.gap_reasons for item in uncovered))

    def test_dynamic_claims_fail_closed_for_unknown_or_non_executable_cases(self):
        with self.assertRaisesRegex(CoverageError, "not in the plan"):
            build_coverage_report(self.plan, observed_case_ids=["unknown-case"])

        with self.assertRaisesRegex(CoverageError, "non-executable"):
            build_coverage_report(self.plan, observed_case_ids=["upload-case"])

        with self.assertRaisesRegex(CoverageError, "not in the plan"):
            build_coverage_report(
                self.plan,
                observed_requirement_ids=["req.unknown"],
            )

        with self.assertRaisesRegex(CoverageError, "without an executable"):
            build_coverage_report(
                self.plan,
                observed_requirement_ids=[self.upload_requirement_id],
            )

    def test_imported_requirement_evidence_is_not_fabricated_as_a_case_run(self):
        report = build_coverage_report(
            self.plan,
            observed_requirement_ids=[self.local_requirement_id],
        )
        local = next(
            item
            for item in report.requirements
            if item.requirement_id == self.local_requirement_id
        )

        self.assertTrue(local.observed)
        self.assertTrue(local.direct_observation)
        self.assertEqual((), local.observed_case_ids)
        self.assertEqual((), report.observed_case_ids)

    def test_round_trip_rejects_tampered_metrics_and_unknown_fields(self):
        report = build_coverage_report(
            self.plan,
            observed_case_ids=["local-case"],
        )

        self.assertEqual(report, CoverageReport.from_json(report.to_json()))
        self.assertEqual(report.to_dict(), json.loads(report.to_json()))

        invalid = report.to_dict()
        invalid["planned"]["covered"] += 1
        with self.assertRaises(CoverageError):
            CoverageReport.from_dict(invalid)

        invalid = report.to_dict()
        invalid["unexpected"] = True
        with self.assertRaisesRegex(CoverageError, "unsupported fields"):
            CoverageReport.from_dict(invalid)

        invalid = report.to_dict()
        invalid["requirements"] = "not-an-array"
        with self.assertRaisesRegex(CoverageError, "JSON array"):
            CoverageReport.from_dict(invalid)

        with self.assertRaisesRegex(CoverageError, "invalid.*JSON"):
            CoverageReport.from_json('{"api_version":"x","api_version":"x"}')


if __name__ == "__main__":
    unittest.main()
