import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from aceval.code_review import create_code_review_fixture_lab
from aceval.online_code_review import (
    EXPECTATIONS,
    build_online_review_benchmark,
    build_online_review_prompt,
    load_online_review_cases,
    run_online_review_suite,
)


class OnlineCodeReviewTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.lab = Path(self.temporary.name, "lab")
        create_code_review_fixture_lab(self.lab)

    def tearDown(self):
        self.temporary.cleanup()

    def test_selects_nine_frontend_cases_and_builds_frozen_prompt(self):
        cases = load_online_review_cases(self.lab)
        self.assertEqual(9, len(cases))
        self.assertEqual(
            {"typescript-web", "react-native", "wechat-miniprogram"},
            {item.stack for item in cases},
        )
        case = cases[0]
        skill_commit = "e" * 40
        prompt = build_online_review_prompt(
            case,
            skill_ref="feature/test-and-optimize",
            skill_commit=skill_commit,
        )
        self.assertIn(skill_commit, prompt)
        self.assertIn(case.base_commit, prompt)
        self.assertIn(case.head_commit, prompt)
        self.assertIn("只能是严格 JSON", prompt)

    def test_selects_explicit_case_and_defines_binding_assertion(self):
        cases = load_online_review_cases(
            self.lab, case_ids=("rn-listener-leak",)
        )
        self.assertEqual(["rn-listener-leak"], [item.id for item in cases])
        self.assertTrue(any("commits" in item for item in EXPECTATIONS))

    def test_benchmark_uses_timing_tokens_and_candidate_minus_baseline(self):
        workspace = Path(self.temporary.name, "workspace")
        eval_dir = workspace / "eval-01-case"
        (eval_dir / "eval_metadata.json").parent.mkdir(parents=True)
        (eval_dir / "eval_metadata.json").write_text(
            json.dumps({"eval_id": 1, "eval_name": "case"})
        )
        for configuration, pass_rate, formal, tokens in (
            ("with_skill", 1.0, "pass", 120),
            ("old_skill", 0.2, "fail", 300),
        ):
            run_dir = eval_dir / configuration / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "grading.json").write_text(
                json.dumps(
                    {
                        "summary": {
                            "pass_rate": pass_rate,
                            "passed": int(pass_rate * 5),
                            "failed": 5 - int(pass_rate * 5),
                            "total": 5,
                        },
                        "formal_grade": {"status": formal},
                        "execution_metrics": {
                            "total_tool_calls": 2,
                            "errors_encountered": 0,
                            "output_chars": 9999,
                        },
                        "expectations": [],
                    }
                )
            )
            (run_dir / "timing.json").write_text(
                json.dumps(
                    {
                        "total_duration_seconds": 10.0,
                        "total_tokens": tokens,
                    }
                )
            )
        benchmark = build_online_review_benchmark(workspace)
        self.assertEqual(1, benchmark["metadata"]["runs_per_configuration"])
        self.assertEqual(120, benchmark["run_summary"]["with_skill"]["totals"]["tokens"])
        self.assertEqual("+0.8000", benchmark["run_summary"]["delta"]["pass_rate"])
        self.assertEqual("-180", benchmark["run_summary"]["delta"]["tokens"])

    def test_online_suite_resolves_a_versioned_path_catalog_per_case(self):
        selected = load_online_review_cases(self.lab)[:2]
        catalog = {
            "api_version": "aceval.kernel-execution-paths/v1",
            "paths": {
                item.id: {
                    "api_version": "aceval.execution-path-spec/v1",
                    "steps": [
                        {
                            "id": "path-%s" % item.id,
                            "label": "Path for %s" % item.id,
                            "kind": "required",
                            "match": {"contains": item.id},
                        }
                    ],
                }
                for item in selected
            },
        }
        observed = {}

        def fake_run(_client, case, **kwargs):
            observed[case.id] = kwargs["execution_path"].steps[0].id
            return {
                "case_id": case.id,
                "session_id": "session-%s" % case.id,
                "status": "COMPLETED",
                "error": None,
                "pass_rate": 1.0,
                "formal_status": "pass",
                "binding_verified": True,
                "duration_seconds": 1.0,
                "total_tokens": 10,
                "path_conformance": None,
            }

        workspace = Path(self.temporary.name, "catalog-workspace")
        with mock.patch(
            "aceval.online_code_review.CatxAgentClient", return_value=object()
        ), mock.patch(
            "aceval.online_code_review.run_online_review_case",
            side_effect=fake_run,
        ):
            summary = run_online_review_suite(
                None,
                lab_root=self.lab,
                skill_ref="feature/test",
                skill_commit="e" * 40,
                workspace=workspace,
                configuration="with_skill",
                case_ids=tuple(item.id for item in selected),
                execution_path=catalog,
            )

        self.assertEqual(2, len(summary["cases"]))
        self.assertEqual(
            {item.id: "path-%s" % item.id for item in selected}, observed
        )

    def test_online_suite_validates_all_catalog_paths_before_starting_sessions(self):
        selected = load_online_review_cases(self.lab)[:2]
        catalog = {
            "paths": {
                selected[0].id: {
                    "api_version": "aceval.execution-path-spec/v1",
                    "steps": [
                        {
                            "id": "load",
                            "label": "Load",
                            "kind": "required",
                            "match": {"contains": "SKILL.md"},
                        }
                    ],
                },
                selected[1].id: {
                    "api_version": "aceval.execution-path-spec/v1",
                    "steps": [],
                },
            }
        }
        with mock.patch(
            "aceval.online_code_review.CatxAgentClient"
        ) as client, mock.patch(
            "aceval.online_code_review.run_online_review_case"
        ) as run_case:
            with self.assertRaises(ValueError):
                run_online_review_suite(
                    None,
                    lab_root=self.lab,
                    skill_ref="feature/test",
                    skill_commit="e" * 40,
                    workspace=Path(self.temporary.name, "invalid-catalog"),
                    configuration="with_skill",
                    case_ids=tuple(item.id for item in selected),
                    execution_path=catalog,
                )
        client.assert_not_called()
        run_case.assert_not_called()
