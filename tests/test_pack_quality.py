import tempfile
import unittest
from pathlib import Path

from aceval.pack_builder import PackCalibrationError, freeze_evalpack, generate_evalpack
from aceval.pack_quality import evaluate_pack_quality


class PackQualityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def design(self, requirements):
        subject_hash = "sha256:" + "a" * 64
        return {
            "capability_graph": {
                "api_version": "aceval.skill-analysis/v1",
                "subject_hash": subject_hash,
                "capabilities": [{"id": "cap.answer"}],
            },
            "test_plan": {
                "api_version": "aceval.test-plan/v1",
                "subject_hash": subject_hash,
                "requirements": requirements,
                "runtime_gaps": [],
            },
            "coverage_target": {
                "api_version": "aceval.coverage-report/v1",
                "subject_hash": subject_hash,
                "planned": {},
            },
            "generation_provenance": {
                "api_version": "aceval.case-generation/v1",
                "subject_hash": subject_hash,
                "mutation_score": None,
            },
        }

    def case(self, requirement_ids=("req.answer.happy",), **overrides):
        value = {
            "id": "answer-dev",
            "prompt": "Return the exact JSON answer.",
            "expected_output": {"answer": 42},
            "metadata": {
                "aceval_test": {
                    "requirement_ids": list(requirement_ids),
                    "family_id": "answer-happy",
                    "origin": "seed",
                    "oracle_trust": "seed_derived",
                    "executable": True,
                }
            },
        }
        value.update(overrides)
        return value

    def requirement(self, requirement_id="req.answer.happy", priority="critical"):
        return {
            "id": requirement_id,
            "capability_id": "cap.answer",
            "priority": priority,
        }

    def test_ready_design_sidecars_are_hashed_and_can_freeze(self):
        result = generate_evalpack(
            {"cases": [self.case()]},
            "generic",
            "Return the correct answer.",
            self.root / "ready",
            test_design=self.design([self.requirement()]),
        )

        self.assertTrue((result.root / "design" / "test-plan.json").is_file())
        report = evaluate_pack_quality(result.root)
        self.assertTrue(report.ready_for_freeze)
        self.assertEqual(1.0, report.planned_coverage)
        self.assertEqual(1.0, report.oracle_ready_coverage)
        self.assertTrue(freeze_evalpack(result.root, approve=True).trusted)

    def test_uncovered_critical_requirement_blocks_freeze(self):
        design = self.design(
            [
                self.requirement(),
                self.requirement("req.answer.negative", "critical"),
            ]
        )
        result = generate_evalpack(
            {"cases": [self.case()]},
            "generic",
            "Return the correct answer.",
            self.root / "blocked",
            test_design=design,
        )

        report = evaluate_pack_quality(result.root)
        self.assertFalse(report.ready_for_freeze)
        self.assertIn(
            "requirement.critical_uncovered",
            {item.code for item in report.blockers},
        )
        with self.assertRaisesRegex(PackCalibrationError, "Pack Quality blockers"):
            freeze_evalpack(result.root, approve=True)

    def test_generated_holdout_and_untrusted_oracle_are_blockers(self):
        case = self.case(
            split="holdout",
            metadata={
                "aceval_test": {
                    "requirement_ids": ["req.answer.happy"],
                    "family_id": "answer-happy",
                    "origin": "requirement_synthesis",
                    "oracle_trust": "model_proposed",
                    "executable": True,
                }
            },
        )
        result = generate_evalpack(
            {
                "cases": [
                    self.case(),
                    dict(case, id="answer-holdout"),
                ]
            },
            "generic",
            "Return the correct answer.",
            self.root / "holdout",
            test_design=self.design([self.requirement()]),
        )

        report = evaluate_pack_quality(result.root)
        codes = {item.code for item in report.blockers}
        self.assertIn("case.generated_holdout", codes)
        self.assertIn("case.untrusted_oracle", codes)

    def test_generation_provenance_plan_hash_mismatch_blocks_freeze(self):
        design = self.design([self.requirement()])
        design["generation_provenance"]["test_plan_hash"] = (
            "sha256:" + "0" * 64
        )
        result = generate_evalpack(
            {"cases": [self.case()]},
            "generic",
            "Return the correct answer.",
            self.root / "provenance-mismatch",
            test_design=design,
        )

        report = evaluate_pack_quality(result.root)

        self.assertFalse(report.ready_for_freeze)
        self.assertIn(
            "test_design.plan_hash_mismatch",
            {item.code for item in report.blockers},
        )
        with self.assertRaisesRegex(PackCalibrationError, "Pack Quality blockers"):
            freeze_evalpack(result.root, approve=True)

    def test_design_sidecars_reject_duplicate_keys_and_nonstandard_numbers(self):
        invalid_documents = (
            (
                "duplicate",
                '{"api_version":"aceval.case-generation/v1",'
                '"test_plan_hash":"sha256:first",'
                '"test_plan_hash":"sha256:second"}',
            ),
            (
                "nan",
                '{"api_version":"aceval.case-generation/v1",'
                '"test_plan_hash":"sha256:value","mutation_score":NaN}',
            ),
        )
        for name, invalid in invalid_documents:
            with self.subTest(name=name):
                result = generate_evalpack(
                    {"cases": [self.case()]},
                    "generic",
                    "Return the correct answer.",
                    self.root / name,
                    test_design=self.design([self.requirement()]),
                )
                (result.root / "design" / "generation-provenance.json").write_text(
                    invalid,
                    encoding="utf-8",
                )

                report = evaluate_pack_quality(result.root)
                self.assertFalse(report.ready_for_freeze)
                self.assertIn(
                    "test_design.invalid_ref",
                    {item.code for item in report.blockers},
                )

    def test_sidecar_versions_and_subject_hashes_fail_closed(self):
        design = self.design([self.requirement()])
        design["test_plan"]["api_version"] = "aceval.test-plan/v2"
        design["coverage_target"]["subject_hash"] = "not-a-hash"
        result = generate_evalpack(
            {"cases": [self.case()]},
            "generic",
            "Return the correct answer.",
            self.root / "invalid-contract",
            test_design=design,
        )

        report = evaluate_pack_quality(result.root)
        codes = {item.code for item in report.blockers}
        self.assertIn("test_design.unsupported_sidecar_version", codes)
        self.assertIn("test_design.invalid_subject_hash", codes)

    def test_explicitly_pending_oracle_cannot_freeze_even_with_an_assertion(self):
        case = self.case()
        case["metadata"]["aceval_test"]["oracle_ready"] = False
        case["metadata"]["aceval_test"]["needs_user_input"] = True
        result = generate_evalpack(
            {"cases": [case]},
            "generic",
            "Return the correct answer.",
            self.root / "pending-oracle",
            test_design=self.design([self.requirement()]),
        )

        report = evaluate_pack_quality(result.root)
        self.assertIn(
            "case.oracle_pending",
            {item.code for item in report.blockers},
        )


if __name__ == "__main__":
    unittest.main()
