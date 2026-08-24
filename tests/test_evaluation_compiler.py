import json
from pathlib import Path
import tempfile
import unittest

from aceval.evaluation_compiler import (
    classify_evaluation,
    compile_evaluation,
    profile_catalog,
)
from aceval.pack_builder import freeze_evalpack


class EvaluationCompilerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def skill(self, text=None):
        root = self.root / ("skill-%d" % len(list(self.root.glob("skill-*"))))
        root.mkdir()
        (root / "SKILL.md").write_text(
            text
            or """# Answer Skill

## Return structured answer

Inputs:
- a user request

Outputs:
- a JSON answer

Return the requested answer as JSON.
""",
            encoding="utf-8",
        )
        return root

    def test_case_free_compilation_requests_only_missing_expected_result(self):
        result = compile_evaluation(
            self.skill(),
            "Return a correct structured answer.",
            self.root / "generated",
            standards="The result must be exact JSON.",
        )

        self.assertEqual("generated", result.source)
        self.assertEqual("needs_user_input", result.status)
        self.assertEqual("expected_result", result.required_inputs[0]["field"])
        self.assertTrue((result.pack.root / "pack.yaml").is_file())
        manifest = json.loads(
            (result.pack.root / "pack.yaml").read_text(encoding="utf-8")
        )
        self.assertNotIn("optimizer_policy", manifest)
        self.assertEqual(
            result.signature,
            manifest["metadata"]["evaluation_profile"]["signature"],
        )

    def test_user_expected_output_makes_minimal_draft_reviewable(self):
        result = compile_evaluation(
            self.skill(),
            "Return a correct answer.",
            self.root / "expected",
            standards="The answer must equal 42.",
            prompt="Return the answer 42 as JSON.",
            expected_output={"answer": 42},
            has_expected_output=True,
        )

        self.assertEqual("evaluation_review_required", result.status)
        self.assertEqual((), result.required_inputs)
        self.assertEqual("generic-work-product", result.profile.id)

    def test_frozen_exact_suite_is_reused_without_creating_another_pack(self):
        subject = self.skill()
        first = compile_evaluation(
            subject,
            "Return a correct answer.",
            self.root / "first",
            standards="The answer must equal 42.",
            prompt="Return the answer 42 as JSON.",
            expected_output={"answer": 42},
            has_expected_output=True,
        )
        freeze_evalpack(first.pack.root, approve=True)

        second_path = self.root / "second"
        second = compile_evaluation(
            subject,
            "Return a correct answer.",
            second_path,
            standards="The answer must equal 42.",
            prompt="Return the answer 42 as JSON.",
            expected_output={"answer": 42},
            has_expected_output=True,
            reuse_roots=(self.root,),
        )

        self.assertEqual("reused", second.source)
        self.assertEqual("ready", second.status)
        self.assertEqual(first.pack.root, second.pack.root)
        self.assertFalse(second_path.exists())
        self.assertTrue(second.experiment_path.is_file())

    def test_reuse_is_exact_not_merely_same_category(self):
        subject = self.skill()
        first = compile_evaluation(
            subject,
            "Return a correct answer.",
            self.root / "first-standard",
            standards="The answer must equal 42.",
            prompt="Return JSON.",
            expected_output={"answer": 42},
            has_expected_output=True,
        )
        freeze_evalpack(first.pack.root, approve=True)
        second = compile_evaluation(
            subject,
            "Return a correct answer.",
            self.root / "different-standard",
            standards="The answer must equal 7.",
            prompt="Return JSON.",
            expected_output={"answer": 7},
            has_expected_output=True,
            reuse_roots=(self.root,),
        )

        self.assertEqual("generated", second.source)
        self.assertNotEqual(first.signature, second.signature)

    def test_default_output_hint_gets_signature_suffix_on_semantic_change(self):
        subject = self.skill()
        output_hint = self.root / "automatic"
        first = compile_evaluation(
            subject,
            "Return a correct answer.",
            output_hint,
            standards="Use the declared JSON format.",
            expected_output={"answer": 42},
            has_expected_output=True,
            reuse=False,
            allow_output_variant=True,
        )
        second = compile_evaluation(
            subject,
            "Return a correct answer.",
            output_hint,
            standards="Use the declared JSON format.",
            expected_output={"answer": 7},
            has_expected_output=True,
            reuse=False,
            allow_output_variant=True,
        )

        self.assertEqual(output_hint.resolve(), first.pack.root)
        self.assertNotEqual(first.pack.root, second.pack.root)
        self.assertTrue(second.pack.root.name.startswith("automatic-"))

    def test_common_work_profiles_are_inferred_internally(self):
        scenarios = {
            "design-to-code": "Convert a Figma 设计稿 into responsive frontend code.",
            "code-review": "Perform frontend code review and grounded 代码评审.",
            "online-document-edit": "Edit a 学城 Citadel Markdown document.",
            "data-query": "Use SQL to query Hive and export BI results.",
            "observability-diagnosis": "Analyze Raptor 告警 and identify 根因.",
            "workflow-transaction": "Create an approval and update a TT 工单.",
        }
        for expected, description in scenarios.items():
            profile, _ = classify_evaluation(self.skill(description), description)
            self.assertEqual(expected, profile.id)
        self.assertIn(
            "authentication-dependency",
            {item["id"] for item in profile_catalog()},
        )


if __name__ == "__main__":
    unittest.main()
