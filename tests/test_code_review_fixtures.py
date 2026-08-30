import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from aceval.agent_runtime import ModelReply
from aceval.code_review_fixtures import CodeReviewFixtureGenerationError, _changes, generate_code_review_fixtures


class FixtureModel:
    def complete(self, messages, tools):
        request = messages[-1]["content"]
        self.assertions(request)
        return ModelReply(content=json.dumps({
            "api_version": "aceval.code-review-fixture-generation/v1",
            "changes": [{
                "path": "src/service.py",
                "operation": "replace_text",
                "old_text": "return 'safe'",
                "new_text": "return user_input",
                "reason": "introduce a reviewable data-flow defect",
            }],
            "rationale": "The Case needs an unsafe data-flow change.",
        }))

    @staticmethod
    def assertions(prompt):
        if "Read the Skill" not in prompt or "Review the user input" not in prompt:
            raise AssertionError("fixture prompt did not include Skill and Case context")


class CodeReviewFixtureTests(unittest.TestCase):
    def test_create_file_accepts_model_new_text_alias(self):
        changes, _ = _changes({"changes": [{"path": "src/new.py", "operation": "create_file", "new_text": "print('ok')"}]})
        self.assertEqual("print('ok')", changes[0]["content"])

    def test_generates_isolated_branch_and_commit_without_dirtying_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origin = root / "origin.git"
            repo = root / "repo"
            subprocess.run(("git", "init", "--bare", str(origin)), check=True, stdout=subprocess.PIPE)
            subprocess.run(("git", "clone", str(origin), str(repo)), check=True, stdout=subprocess.PIPE)
            subprocess.run(("git", "config", "user.email", "forge@example.test"), cwd=repo, check=True)
            subprocess.run(("git", "config", "user.name", "FORGE"), cwd=repo, check=True)
            (repo / "src").mkdir()
            (repo / "src/service.py").write_text("def handle(user_input):\n    return 'safe'\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-m", "base"), cwd=repo, check=True, stdout=subprocess.PIPE)
            subprocess.run(("git", "push", "origin", "HEAD:master"), cwd=repo, check=True, stdout=subprocess.PIPE)
            base = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=repo, text=True).strip()
            result = generate_code_review_fixtures(
                FixtureModel(), repository=repo, skill_text="Read the Skill before acting.",
                cases=({"id": "case-one", "prompt": "Review the user input"},),
                output_root=root / "artifacts", push=False,
            )[0]
            self.assertEqual(base, result.base_commit)
            self.assertEqual("aceval/case/case-one", result.branch)
            self.assertNotEqual(base, result.head_commit)
            self.assertEqual("    return 'safe'", (repo / "src/service.py").read_text(encoding="utf-8").splitlines()[1])
            self.assertEqual("    return user_input", subprocess.check_output(("git", "show", result.head_commit + ":src/service.py"), cwd=repo, text=True).splitlines()[1])

    def test_does_not_overwrite_existing_fixture_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(("git", "init", str(root)), check=True, stdout=subprocess.PIPE)
            subprocess.run(("git", "config", "user.email", "forge@example.test"), cwd=root, check=True)
            subprocess.run(("git", "config", "user.name", "FORGE"), cwd=root, check=True)
            (root / "README.md").write_text("base\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src/service.py").write_text("def review(user_input):\n    return 'safe'\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=root, check=True)
            subprocess.run(("git", "commit", "-m", "base"), cwd=root, check=True, stdout=subprocess.PIPE)
            subprocess.run(("git", "branch", "aceval/case/case-one"), cwd=root, check=True)
            result = generate_code_review_fixtures(
                FixtureModel(), repository=root, skill_text="Read the Skill", cases=({"id": "case-one", "prompt": "Review the user input"},), output_root=root / "artifacts", push=False,
            )[0]
            self.assertRegex(result.branch, r"^aceval/case/case-one-\d{14}-[0-9a-f]{8}$")


if __name__ == "__main__":
    unittest.main()
