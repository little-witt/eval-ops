import hashlib
import json
import unittest

from aceval.case_generation import CaseGenerationError, compile_case_drafts
from aceval.skill_analysis import analyze_skill
from aceval.test_planning import plan_tests


SKILL = """# Summary Skill

## Create summary

Inputs: `input.csv`
Outputs: `summary.json`

1. Use `read_file` to read `input.csv`.
2. Use `write_file` to create `summary.json`.
3. If the input is empty, return an explicit empty summary.
"""


RUNTIME_CAPABILITIES = {
    "fresh_session",
    "workspace_fixture",
    "artifact_output",
    "canonical_trace",
    "stateful_replay",
}


def canonical_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class CaseGenerationTest(unittest.TestCase):
    def setUp(self):
        self.seed_cases = [
            {
                "id": "summary-seed",
                "prompt": "Create summary.json from input.csv.",
                "fixtures": [
                    {"path": "input.csv", "content": "value\n1\n"}
                ],
                "expected_output": {"count": 1},
                "expected_observables": ["summary.json contains one row"],
                "family_id": "summary-family",
                "oracle_level": "human_confirmed",
                "required_runtime_capabilities": ["artifact_output"],
                "metadata": {"owner": "user"},
            }
        ]
        self.plan = plan_tests(
            analyze_skill(SKILL),
            seed_cases=self.seed_cases,
            goal="Cover normal and empty input without changing source data.",
            runtime_capabilities=RUNTIME_CAPABILITIES,
            max_generated_cases=2,
        )

    def test_compiles_seed_and_generated_case_drafts_with_provenance(self):
        result = compile_case_drafts(
            self.plan,
            self.seed_cases,
            name="summary-planned",
            version="0.2.0",
        )

        self.assertEqual("aceval.case-generation/v1", result.api_version)
        self.assertEqual("summary-planned", result.cases_document["name"])
        self.assertEqual("0.2.0", result.cases_document["version"])
        self.assertEqual(
            tuple(case.id for case in self.plan.cases),
            result.active_case_ids,
        )
        self.assertTrue(result.generated_case_ids)

        by_id = {
            case["id"]: case for case in result.cases_document["cases"]
        }
        seed = by_id["summary-seed"]
        self.assertEqual(self.seed_cases[0]["fixtures"], seed["fixtures"])
        self.assertEqual({"count": 1}, seed["expected_output"])
        self.assertEqual("user", seed["metadata"]["owner"])
        for field in (
            "expected_observables",
            "family_id",
            "oracle_level",
            "required_runtime_capabilities",
            "requirement_ids",
        ):
            self.assertNotIn(field, seed)
        self.assertEqual(
            "seed", seed["metadata"]["aceval_test"]["origin"]
        )
        self.assertEqual(
            "summary-family",
            seed["metadata"]["aceval_test"]["family_id"],
        )
        self.assertTrue(seed["metadata"]["aceval_test"]["oracle_ready"])

        for case_id in result.generated_case_ids:
            generated = by_id[case_id]
            self.assertNotIn("expected_output", generated)
            self.assertIn("aceval-generated", generated["tags"])
            self.assertEqual(
                "requirement_synthesis",
                generated["metadata"]["aceval_test"]["origin"],
            )
            self.assertIn(case_id, result.pending_oracle_case_ids)

        provenance = result.generation_provenance
        self.assertEqual(self.plan.subject_hash, provenance["subject_hash"])
        self.assertEqual(
            canonical_hash(self.plan.to_dict()),
            provenance["test_plan_hash"],
        )
        self.assertEqual(
            canonical_hash(self.seed_cases),
            provenance["seed_cases_hash"],
        )
        self.assertEqual(len(self.plan.cases), provenance["case_count"])
        self.assertEqual(
            len(result.generated_case_ids), provenance["generated_case_count"]
        )

    def test_rejects_missing_seed_and_reserved_metadata(self):
        with self.assertRaisesRegex(CaseGenerationError, "missing from source"):
            compile_case_drafts(self.plan, ())

        invalid_seed = json.loads(json.dumps(self.seed_cases))
        invalid_seed[0]["metadata"]["aceval_test"] = {"forged": True}
        with self.assertRaisesRegex(CaseGenerationError, "reserved"):
            compile_case_drafts(self.plan, invalid_seed)

        oracle_ref_seed = json.loads(json.dumps(self.seed_cases))
        oracle_ref_seed[0]["oracle_ref"] = "oracles/summary.json"
        with self.assertRaisesRegex(CaseGenerationError, "cannot snapshot"):
            compile_case_drafts(self.plan, oracle_ref_seed)


if __name__ == "__main__":
    unittest.main()
