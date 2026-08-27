import json
import tempfile
import unittest
from pathlib import Path

from aceval.agent_runtime import ModelReply
from aceval.model_case_generation import ModelCaseGenerationError, build_case_generation_repair_prompt, parse_model_case_design
from aceval.skill_analysis import analyze_skill
from aceval.test_planning import plan_tests


class ModelCaseGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name, "SKILL.md")
        path.write_text(
            "# Review\n\n## Capabilities\n\n### Review source\n\nInputs:\n- repository\n\nOutputs:\n- report\n\nSteps:\n1. Read source.\n2. Return report.\n\n### Summarize findings\n\nInputs:\n- report\n\nOutputs:\n- summary\n\nSteps:\n1. Summarize report.\n2. Return summary.\n",
            encoding="utf-8",
        )
        self.graph = analyze_skill(path)
        self.plan = plan_tests(
            self.graph,
            goal="review source",
            runtime_capabilities=("headless", "fresh_session", "workspace_fixture", "artifact_output", "canonical_trace", "fixed_parameters", "skill_activation"),
            max_generated_cases=2,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _design(self, duplicate=False):
        cases = []
        paths = []
        for index, planned in enumerate(self.plan.cases):
            requirement = next(item for item in self.plan.requirements if item.id in planned.requirement_ids)
            cases.append({
                "id": planned.id,
                "title": "Review happy path" if index == 0 else "Review boundary",
                "prompt": ("Read the repository and produce a grounded report." if index == 0 or duplicate else "Summarize the report and return a concise summary."),
                "family": planned.family,
                "kind": planned.family.rsplit(".", 1)[-1],
                "requirement_ids": [requirement.id],
                "source_refs": [requirement.source_refs[0]],
                "generation_reason": "Cover the declared requirement",
                "expected_observables": list(requirement.expected_observables),
                "oracle_strategy": requirement.oracle_strategy,
                "required_runtime_capabilities": [],
            })
            paths.append({"case_id": planned.id, "purpose": "Case-specific path", "steps": [{"id": "read", "label": "Read Skill", "kind": "required", "match": {"contains": "SKILL.md"}, "after": []}]})
        return {"api_version": "aceval.model-case-design/v1", "cases": cases, "paths": paths, "generation_summary": {"strategy": "fake"}}

    def test_parser_preserves_distinct_source_grounded_cases(self):
        value = parse_model_case_design(json.dumps(self._design()), plan=self.plan, target_case_ids=[item.id for item in self.plan.cases])
        self.assertEqual(2, len(value["cases"]))
        self.assertTrue(all(item["source_refs"] for item in value["cases"]))

    def test_parser_rejects_duplicate_prompts(self):
        with self.assertRaisesRegex(ModelCaseGenerationError, "duplicate Case prompts"):
            parse_model_case_design(json.dumps(self._design(duplicate=True)), plan=self.plan, target_case_ids=[item.id for item in self.plan.cases])

    def test_parser_normalizes_readable_family_and_planner_owned_dimension(self):
        design = self._design()
        design["cases"][0]["family"] = "代码审查 · 证据引用"
        design["cases"][0]["kind"] = "artifact presence"
        value = parse_model_case_design(json.dumps(design), plan=self.plan, target_case_ids=[item.id for item in self.plan.cases])
        self.assertEqual("代码审查 · 证据引用", value["cases"][0]["family"])
        self.assertEqual("happy_path", value["cases"][0]["kind"])
        self.assertEqual("artifact presence", value["cases"][0]["model_kind"])

    def test_repair_prompt_is_bounded_and_requires_full_revalidation(self):
        prompt = build_case_generation_repair_prompt(validation_error="invalid JSON", invalid_response="x" * 60000)
        self.assertLess(len(prompt), 50000)
        self.assertIn("从头重新经过", prompt)
        self.assertIn("invalid JSON", prompt)


if __name__ == "__main__":
    unittest.main()
