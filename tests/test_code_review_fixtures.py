import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from aceval.agent_runtime import ModelReply
from aceval.code_review_fixtures import CodeReviewFixtureGenerationError, _changes, _fallback_fixture_payload, _json_response, generate_code_review_fixtures


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


class RetryFixtureModel(FixtureModel):
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelReply(content="Here is the patch:\nnot-json")
        return super().complete(messages, tools)


class CodeReviewFixtureTests(unittest.TestCase):
    def test_json_response_accepts_fence_and_short_prose_wrapper(self):
        value = _json_response(
            "已生成补丁：\n```json\n{\"changes\": []}\n```\n请检查。"
        )
        self.assertEqual([], value["changes"])

    def test_create_file_accepts_model_new_text_alias(self):
        changes, _ = _changes({"changes": [{"path": "src/new.py", "operation": "create_file", "new_text": "print('ok')"}]})
        self.assertEqual("print('ok')", changes[0]["content"])

    def test_changes_accepts_single_edit_object_shorthand(self):
        changes, _ = _changes({"changes": {"path": "src/new.py", "operation": "create_file", "content": "print('ok')"}})
        self.assertEqual("src/new.py", changes[0]["path"])

    def test_empty_model_response_gets_deterministic_source_fallback(self):
        payload = _fallback_fixture_payload("\n--- src/service.ts ---\nexport const value = 1;\n", {"id": "case-one"})
        changes, rationale = _changes(payload)
        self.assertEqual("create_file", changes[0]["operation"])
        self.assertTrue(changes[0]["path"].startswith("src/aceval-fixture-"))
        self.assertIn("连续未返回", rationale)

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

    def test_retries_once_when_fixture_model_returns_invalid_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(("git", "init", str(repo)), check=True, stdout=subprocess.PIPE)
            subprocess.run(("git", "config", "user.email", "forge@example.test"), cwd=repo, check=True)
            subprocess.run(("git", "config", "user.name", "FORGE"), cwd=repo, check=True)
            (repo / "src").mkdir()
            (repo / "src/service.py").write_text("def handle(user_input):\n    return 'safe'\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-m", "base"), cwd=repo, check=True, stdout=subprocess.PIPE)
            model = RetryFixtureModel()
            result = generate_code_review_fixtures(
                model,
                repository=repo,
                skill_text="Read the Skill before acting.",
                cases=({"id": "retry-case", "prompt": "Review the user input"},),
                output_root=root / "artifacts",
                push=False,
            )
            self.assertEqual(1, len(result))
            self.assertEqual(2, model.calls)
            self.assertNotEqual(result[0].base_commit, result[0].head_commit)


if __name__ == "__main__":
    unittest.main()
