import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from aceval.code_review import (
    CodeReviewFindingsGrader,
    CodeReviewError,
    DEFAULT_CODE_REVIEW_CASE,
    DEFAULT_CODE_REVIEW_STACKS,
    STACK_REACT_NATIVE,
    create_code_review_fixture_lab,
    infer_code_review_stacks,
    repository_observation,
)
from aceval.contracts import GradeStatus, Oracle


class CodeReviewP0Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name, "lab")
        self.cases = create_code_review_fixture_lab(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def grade(self, case, findings):
        oracle = json.loads(case.oracle.read_text(encoding="utf-8"))
        observation = repository_observation(
            case.repository,
            {"findings": findings},
            revision=case.head_commit,
        )
        return asyncio.run(
            CodeReviewFindingsGrader().evaluate(observation, Oracle(data=oracle), {})
        )

    def test_lab_has_isolated_reproducible_git_cases_and_evals(self):
        self.assertEqual(12, len(self.cases))
        self.assertEqual(set(DEFAULT_CODE_REVIEW_STACKS), {case.stack for case in self.cases})
        self.assertEqual(1, len({case.repository for case in self.cases}))
        for stack in DEFAULT_CODE_REVIEW_STACKS:
            stack_cases = [case for case in self.cases if case.stack == stack]
            self.assertEqual(3, len(stack_cases))
            self.assertEqual(2, sum(case.case_type == "defect" for case in stack_cases))
            self.assertEqual(1, sum(case.case_type == "clean" for case in stack_cases))

        evals = json.loads(Path(self.root, "evals", "evals.json").read_text(encoding="utf-8"))
        self.assertEqual(12, len(evals["evals"]))
        self.assertTrue(all("expectations" in item for item in evals["evals"]))
        self.assertTrue(all("assertions" not in item for item in evals["evals"]))
        self.assertTrue(
            all(not Path(item["files"][0]).is_absolute() for item in evals["evals"])
        )

        lab = json.loads(Path(self.root, "lab.json").read_text(encoding="utf-8"))
        self.assertEqual(list(DEFAULT_CODE_REVIEW_STACKS), lab["stacks"])
        self.assertEqual(DEFAULT_CODE_REVIEW_CASE, lab["default_case"])
        self.assertEqual("repository", lab["repository"])
        self.assertEqual("master", lab["default_branch"])
        self.assertTrue(
            all(
                lab["coverage"][stack]
                == {"cases": 3, "defect_cases": 2, "clean_cases": 1}
                for stack in DEFAULT_CODE_REVIEW_STACKS
            )
        )
        self.assertTrue(all(case.base_commit != case.head_commit for case in self.cases))
        for case in self.cases:
            completed = subprocess.run(
                ("git", "diff", "--name-only", case.base_commit, case.head_commit),
                cwd=str(case.repository),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr.decode())
            changed = completed.stdout.decode().strip().splitlines()
            self.assertEqual(1, len(changed), case.id)
            self.assertEqual(
                case.base_commit,
                subprocess.run(
                    ("git", "rev-parse", case.base_ref),
                    cwd=str(case.repository),
                    stdout=subprocess.PIPE,
                    check=True,
                    shell=False,
                ).stdout.decode().strip(),
            )
            self.assertEqual(
                case.head_commit,
                subprocess.run(
                    ("git", "rev-parse", case.head_ref),
                    cwd=str(case.repository),
                    stdout=subprocess.PIPE,
                    check=True,
                    shell=False,
                ).stdout.decode().strip(),
            )
            status = subprocess.run(
                ("git", "status", "--porcelain"),
                cwd=str(case.repository),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
            )
            self.assertEqual(b"", status.stdout, case.id)
        default_case = next(case for case in self.cases if case.id == DEFAULT_CODE_REVIEW_CASE)
        current_head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=str(default_case.repository),
            stdout=subprocess.PIPE,
            check=True,
            shell=False,
        ).stdout.decode().strip()
        self.assertEqual(default_case.base_commit, current_head)
        visible_stacks = {
            item.name
            for item in (default_case.repository / "fixtures").iterdir()
            if item.is_dir()
        }
        self.assertEqual(set(DEFAULT_CODE_REVIEW_STACKS), visible_stacks)

    def test_stack_filter_and_reproducible_commits(self):
        filtered_root = Path(self.temporary.name, "react-native-only")
        filtered = create_code_review_fixture_lab(
            filtered_root,
            stacks=(STACK_REACT_NATIVE,),
        )
        self.assertEqual(3, len(filtered))
        self.assertTrue(all(case.stack == STACK_REACT_NATIVE for case in filtered))
        repeated = create_code_review_fixture_lab(
            Path(self.temporary.name, "react-native-only-repeat"),
            stacks=(STACK_REACT_NATIVE,),
        )
        self.assertEqual(
            {case.id: (case.base_commit, case.head_commit) for case in filtered},
            {case.id: (case.base_commit, case.head_commit) for case in repeated},
        )
        with self.assertRaisesRegex(CodeReviewError, "unsupported"):
            create_code_review_fixture_lab(
                Path(self.temporary.name, "unknown"),
                stacks=("python-backend",),
            )

    def test_skill_content_selects_specific_or_broad_stacks(self):
        self.assertEqual(
            (STACK_REACT_NATIVE,),
            infer_code_review_stacks("React Native TypeScript code review Skill"),
        )
        self.assertEqual(
            ("java-backend",),
            infer_code_review_stacks("使用 Spring/MyBatis 的 Java 后端代码评审"),
        )
        self.assertEqual(
            ("typescript-web", "react-native", "wechat-miniprogram"),
            infer_code_review_stacks("通用前端代码评审 Skill"),
        )
        self.assertEqual(
            DEFAULT_CODE_REVIEW_STACKS,
            infer_code_review_stacks("通用代码评审 Skill"),
        )

    def test_exact_findings_pass_and_missed_or_invalid_findings_fail(self):
        case = next(item for item in self.cases if item.id == "java-sql-injection")
        expected = json.loads(case.oracle.read_text(encoding="utf-8"))["expected_findings"][0]
        finding = {
            "path": expected["path"],
            "line": expected["line_start"],
            "category": expected["category"],
            "severity": expected["severity"],
            "explanation": "unsafe interpolation",
        }
        passed = self.grade(case, [finding])
        self.assertEqual(GradeStatus.PASS, passed.status)
        self.assertEqual(1.0, passed.metrics["critical_recall"])
        wrong_severity = self.grade(case, [dict(finding, severity="low")])
        self.assertEqual(GradeStatus.FAIL, wrong_severity.status)
        missed = self.grade(case, [])
        self.assertEqual(GradeStatus.FAIL, missed.status)
        invalid = dict(finding, line=999)
        failed = self.grade(case, [invalid])
        self.assertEqual(GradeStatus.FAIL, failed.status)
        self.assertEqual(1, failed.metrics["invalid_source_references"])

    def test_clean_change_requires_no_false_positive(self):
        clean_cases = [item for item in self.cases if item.case_type == "clean"]
        self.assertEqual(4, len(clean_cases))
        for case in clean_cases:
            self.assertEqual(GradeStatus.PASS, self.grade(case, []).status)
            changed_path = subprocess.run(
                ("git", "diff", "--name-only", case.base_commit, case.head_commit),
                cwd=str(case.repository),
                stdout=subprocess.PIPE,
                check=True,
                shell=False,
            ).stdout.decode().strip()
            false_positive = {
                "path": changed_path,
                "line": 1,
                "category": "style",
                "severity": "low",
                "explanation": "not actionable",
            }
            self.assertEqual(GradeStatus.FAIL, self.grade(case, [false_positive]).status)


if __name__ == "__main__":
    unittest.main()
