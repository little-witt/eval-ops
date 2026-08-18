"""Black-box command-line coverage for the reference EvalOps CLI."""

from __future__ import annotations

import contextlib
import io
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

from aceval.cli import main


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "evalpacks" / "csv-summary-smoke"
SECURITY_PACK = ROOT / "evalpacks" / "security-review"
BASELINE = ROOT / "examples" / "subjects" / "csv-summary-skill"
CANDIDATE = ROOT / "examples" / "subjects" / "csv-summary-skill-candidate"


class CliBlackBoxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(
        self, argv: Sequence[str]
    ) -> Tuple[int, str, str, Optional[Any]]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(argv))
        output = stdout.getvalue().strip()
        payload = json.loads(output) if output else None
        return code, output, stderr.getvalue(), payload

    @staticmethod
    def assert_reports(payload: Any) -> None:
        """Every successful workflow advertises files that were actually written."""
        report_json = Path(payload["report_json"])
        report_markdown = Path(payload["report_markdown"])
        if not report_json.is_file():
            raise AssertionError(report_json)
        if not report_markdown.is_file():
            raise AssertionError(report_markdown)
        with report_json.open(encoding="utf-8") as handle:
            report = json.load(handle)
        if report["schema_version"] != "aceval.report/v1":
            raise AssertionError(report)
        if not report_markdown.read_text(encoding="utf-8"):
            raise AssertionError(report_markdown)

    def test_pack_lint_and_fake_pack_test(self) -> None:
        code, _, stderr, payload = self.invoke(["pack", "lint", str(PACK)])
        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("csv-summary-smoke", payload["pack"])
        self.assertEqual(3, payload["scenarios"])

        pack_test_root = self.output_root / "pack-test"
        code, _, stderr, payload = self.invoke(
            [
                "pack",
                "test",
                str(PACK),
                "--runtime",
                "fake",
                "--output-root",
                str(pack_test_root),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertEqual("fake", payload["summary"]["runtime"])
        self.assertEqual(
            ["dev", "validation"],
            [item["split"] for item in payload["summary"]["splits"]],
        )
        self.assert_reports(payload)

        code, _, stderr, payload = self.invoke(
            [
                "pack",
                "test",
                str(SECURITY_PACK),
                "--runtime",
                "fake",
                "--output-root",
                str(self.output_root / "security-pack-test"),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertEqual(
            ["dev", "validation"],
            [item["split"] for item in payload["summary"]["splits"]],
        )
        self.assertNotIn(
            "holdout",
            [item["split"] for item in payload["summary"]["splits"]],
        )

    def test_pack_generate_calibrate_freeze_and_lint_lifecycle(self) -> None:
        cases = self.output_root / "cases.json"
        cases.write_text(
            json.dumps(
                {
                    "name": "generated-cli-pack",
                    "cases": [
                        {
                            "id": "answer-dev",
                            "prompt": "Return JSON.",
                            "expected_output": {"answer": 42},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        pack = self.output_root / "generated-pack"

        code, _, stderr, payload = self.invoke(
            [
                "pack",
                "generate",
                "--type",
                "generic",
                "--cases",
                str(cases),
                "--goal",
                "保持正确并减少 token",
                "--output",
                str(pack),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertEqual("draft", payload["calibration_status"])
        self.assertFalse(payload["optimization_eligible"])
        manifest = json.loads((pack / "pack.yaml").read_text(encoding="utf-8"))
        self.assertEqual(
            "total_tokens",
            manifest["optimizer_policy"]["objective"]["source"]["key"],
        )

        code, _, stderr, payload = self.invoke(["pack", "calibrate", str(pack)])
        self.assertEqual(0, code, stderr)
        self.assertEqual("calibrating", payload["calibration_status"])

        code, output, stderr, payload = self.invoke(["pack", "freeze", str(pack)])
        self.assertEqual(2, code)
        self.assertEqual("", output)
        self.assertIsNone(payload)
        self.assertIn("approve=True", stderr)

        code, _, stderr, payload = self.invoke(
            ["pack", "freeze", str(pack), "--approve"]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["trusted"])
        self.assertTrue((pack / ".aceval-pack-lock.json").is_file())

        code, _, stderr, payload = self.invoke(["pack", "lint", str(pack)])
        self.assertEqual(0, code, stderr)
        self.assertEqual("frozen", payload["calibration_status"])
        self.assertTrue(payload["optimization_eligible"])

    def test_plan_to_pack_quality_and_freeze_workflow(self) -> None:
        skill = self.output_root / "planned-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            """# Answer Skill

## Capabilities

### Return JSON answer

Inputs:
- a question

Outputs:
- a JSON object with an `answer` field

Return the requested answer as JSON.
""",
            encoding="utf-8",
        )
        cases = self.output_root / "planned-cases.json"
        cases.write_text(
            json.dumps(
                {
                    "name": "planned-answer",
                    "cases": [
                        {
                            "id": "answer-dev",
                            "prompt": "Return the answer 42 as JSON.",
                            "expected_output": {"answer": 42},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan = self.output_root / "answer-plan"
        code, _, stderr, payload = self.invoke(
            [
                "plan",
                "--subject",
                str(skill),
                "--cases",
                str(cases),
                "--goal",
                "Return the exact JSON answer.",
                "--output",
                str(plan),
                "--max-generated-cases",
                "0",
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertEqual("needs_user_input", payload["outcome"], payload)
        self.assertTrue(payload["freeze_blockers"])
        self.assertTrue(Path(payload["files"]["test_plan"]).is_file())

        # Simulate the operator resolving the single semantic ambiguity during
        # calibration. The Pack must still preserve the reviewed plan revision.
        plan_document = json.loads(
            (plan / "test-plan.json").read_text(encoding="utf-8")
        )
        plan_document["freeze_blockers"] = []
        plan_document["outcome"] = "ready_for_calibration"
        (plan / "test-plan.json").write_text(
            json.dumps(plan_document), encoding="utf-8"
        )

        code, output, stderr, payload = self.invoke(
            [
                "pack",
                "generate",
                "--plan",
                str(plan),
                "--type",
                "generic",
                "--goal",
                "A different unplanned goal.",
                "--output",
                str(self.output_root / "goal-drift-pack"),
            ]
        )
        self.assertEqual(2, code)
        self.assertEqual("", output)
        self.assertIsNone(payload)
        self.assertIn("does not match the Test Plan", stderr)

        pack = self.output_root / "planned-pack"
        code, _, stderr, payload = self.invoke(
            [
                "pack",
                "generate",
                "--plan",
                str(plan),
                "--type",
                "generic",
                "--output",
                str(pack),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["pack_quality"]["ready_for_freeze"])
        self.assertTrue((pack / "design" / "capability-graph.json").is_file())

        code, _, stderr, payload = self.invoke(["pack", "quality", str(pack)])
        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["test_design_present"])
        self.assertTrue(payload["ready_for_freeze"])

        code, _, stderr, payload = self.invoke(
            ["pack", "freeze", str(pack), "--approve"]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["trusted"])

    def test_doctor_auto_plan_stops_at_visible_calibration_blockers(self) -> None:
        skill = self.output_root / "auto-plan-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            """# Writing Skill

## Write answer

Inputs:
- a user request

Write a good answer as appropriate.
""",
            encoding="utf-8",
        )
        cases = self.output_root / "auto-plan-cases.json"
        cases.write_text(
            json.dumps(
                {
                    "name": "auto-plan-answer",
                    "cases": [
                        {
                            "id": "answer-dev",
                            "prompt": "Write the answer.",
                            "expected_output": {"answer": "ok"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan = self.output_root / "doctor-plan"
        pack = self.output_root / "doctor-pack"

        code, _, stderr, payload = self.invoke(
            [
                "doctor",
                "--subject",
                str(skill),
                "--cases",
                str(cases),
                "--type",
                "generic",
                "--goal",
                "Return a useful JSON answer.",
                "--pack-output",
                str(pack),
                "--auto-plan",
                "--plan-output",
                str(plan),
                "--max-generated-cases",
                "0",
                "--runtime",
                "fake",
                "--output-root",
                str(self.output_root / "doctor-auto-plan-runs"),
            ]
        )

        self.assertEqual(0, code, stderr)
        self.assertEqual("calibration_required", payload["status"])
        self.assertEqual("calibrating", payload["calibration_status"])
        self.assertGreater(payload["planning"]["freeze_blocker_count"], 0)
        self.assertFalse(payload["pack_quality"]["ready_for_freeze"])
        self.assertTrue((plan / "test-plan.json").is_file())
        self.assertTrue((plan / "case-drafts.json").is_file())
        self.assertTrue((pack / "design" / "test-plan.json").is_file())
        self.assertTrue((pack / "design" / "generation-provenance.json").is_file())

    def test_doctor_planned_subject_drift_requires_explicit_override(self) -> None:
        skill = self.output_root / "drift-skill"
        skill.mkdir()
        skill_file = skill / "SKILL.md"
        skill_file.write_text(
            """# Exact Answer Skill

## Return exact JSON answer

Inputs:
- a request

Outputs:
- `response.json`

Use `write_file` to create `response.json` with the exact answer.
""",
            encoding="utf-8",
        )
        (skill / "subject.json").write_text(
            json.dumps({"metadata": {"variant": "baseline"}}),
            encoding="utf-8",
        )
        cases = self.output_root / "drift-cases.json"
        cases.write_text(
            json.dumps(
                {
                    "name": "drift-answer",
                    "cases": [
                        {
                            "id": "answer-dev",
                            "prompt": "Return exact JSON answer.",
                            "expected_output": {"answer": 42},
                            "metadata": {
                                "fake_runtime": {
                                    "variants": {
                                        "baseline": {
                                            "final_output": {"answer": 0}
                                        },
                                        "candidate": {
                                            "final_output": {"answer": 42}
                                        },
                                    }
                                }
                            },
                        },
                        {
                            "id": "answer-validation",
                            "split": "validation",
                            "prompt": "Return the validation JSON answer.",
                            "expected_output": {"answer": 7},
                            "metadata": {
                                "fake_runtime": {
                                    "variants": {
                                        "baseline": {
                                            "final_output": {"answer": 0}
                                        },
                                        "candidate": {
                                            "final_output": {"answer": 7}
                                        },
                                    }
                                }
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan = self.output_root / "drift-plan"
        pack = self.output_root / "drift-pack"

        code, _, stderr, payload = self.invoke(
            [
                "plan",
                "--subject",
                str(skill),
                "--cases",
                str(cases),
                "--goal",
                "Return the exact answer.",
                "--output",
                str(plan),
                "--max-generated-cases",
                "0",
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertEqual("ready_for_calibration", payload["outcome"])

        code, _, stderr, payload = self.invoke(
            [
                "pack",
                "generate",
                "--plan",
                str(plan),
                "--type",
                "generic",
                "--output",
                str(pack),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["pack_quality"]["ready_for_freeze"])
        code, _, stderr, payload = self.invoke(
            ["pack", "freeze", str(pack), "--approve"]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["trusted"])

        skill_file.write_text(
            skill_file.read_text(encoding="utf-8")
            + "\nThe implementation changed after planning.\n",
            encoding="utf-8",
        )
        candidate = self.output_root / "drift-candidate"
        candidate.mkdir()
        (candidate / "SKILL.md").write_text(
            skill_file.read_text(encoding="utf-8")
            + "\nCandidate instruction restores the exact answer behavior.\n",
            encoding="utf-8",
        )
        base_args = [
            "doctor",
            "--subject",
            str(skill),
            "--pack",
            str(pack),
            "--runtime",
            "fake",
            "--candidate",
            str(candidate),
            "--output-root",
            str(self.output_root / "drift-runs"),
        ]

        code, output, stderr, payload = self.invoke(base_args)
        self.assertEqual(2, code)
        self.assertEqual("", output)
        self.assertIsNone(payload)
        self.assertIn("source Subject hash does not match", stderr)
        self.assertIn("--allow-subject-drift", stderr)

        code, output, stderr, payload = self.invoke(
            base_args + ["--allow-subject-drift"]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(output)
        self.assertTrue(payload["accepted"])
        self.assertNotIn("source Subject hash does not match", stderr)

    def test_unknown_pack_type_uses_generic_calibration_fallback(self) -> None:
        cases = self.output_root / "unknown-cases.json"
        cases.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "id": "legal-dev",
                            "prompt": "Return JSON.",
                            "expected_output": {"ok": True},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        pack = self.output_root / "legal-pack"

        code, _, stderr, payload = self.invoke(
            [
                "doctor",
                "--subject",
                str(BASELINE),
                "--cases",
                str(cases),
                "--type",
                "legal-analysis",
                "--goal",
                "结果准确",
                "--pack-output",
                str(pack),
                "--approve-pack",
                "--runtime",
                "fake",
                "--output-root",
                str(self.output_root / "doctor-runs"),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertEqual("calibration_required", payload["status"])
        self.assertEqual("generic", payload["template"])
        self.assertEqual("legal-analysis", payload["requested_type"])
        self.assertEqual("calibrating", payload["calibration_status"])

    def test_run_requires_runtime_and_baseline_candidate_exit_codes(self) -> None:
        # Runtime selection is deliberately mandatory; silently choosing FakeRuntime
        # would make an accidental simulation look like a real evaluation.
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "run",
                        "--pack",
                        str(PACK),
                        "--subject",
                        str(BASELINE),
                    ]
                )
        self.assertEqual(2, raised.exception.code)
        self.assertIn("--runtime", stderr.getvalue())

        baseline_root = self.output_root / "baseline"
        code, _, stderr, baseline = self.invoke(
            [
                "run",
                "--pack",
                str(PACK),
                "--subject",
                str(BASELINE),
                "--split",
                "dev",
                "--runtime",
                "fake",
                "--output-root",
                str(baseline_root),
            ]
        )
        self.assertEqual(1, code, stderr)
        self.assertFalse(baseline["summary"]["passed"])
        self.assertEqual(["dev"], [item["split"] for item in baseline["summary"]["splits"]])
        self.assert_reports(baseline)

        candidate_root = self.output_root / "candidate"
        code, _, stderr, candidate = self.invoke(
            [
                "run",
                "--pack",
                str(PACK),
                "--subject",
                str(CANDIDATE),
                "--split",
                "dev",
                "--runtime",
                "fake",
                "--output-root",
                str(candidate_root),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(candidate["summary"]["passed"])
        self.assertEqual(["dev"], [item["split"] for item in candidate["summary"]["splits"]])
        self.assert_reports(candidate)

    def test_compare_defaults_to_dev_and_marks_simulation(self) -> None:
        code, _, stderr, payload = self.invoke(
            [
                "compare",
                "--pack",
                str(PACK),
                "--subject",
                str(BASELINE),
                "--candidate",
                str(CANDIDATE),
                "--runtime",
                "fake",
                "--output-root",
                str(self.output_root / "compare"),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["simulated"])
        self.assertEqual(
            ["dev"],
            [item["split"] for item in payload["summary"]["baseline"]["splits"]],
        )
        self.assertEqual(
            ["dev"],
            [item["split"] for item in payload["summary"]["candidate"]["splits"]],
        )
        self.assertTrue(payload["accepted"])
        self.assert_reports(payload)
        report = json.loads(Path(payload["report_json"]).read_text(encoding="utf-8"))
        self.assertTrue(report["simulated"])
        self.assertEqual("comparison", report["report_type"])

    def test_fake_optimize_requires_frozen_candidate_and_succeeds_with_one(self) -> None:
        missing_code, _, missing_stderr, missing_payload = self.invoke(
            [
                "optimize",
                "--pack",
                str(PACK),
                "--subject",
                str(BASELINE),
                "--runtime",
                "fake",
                "--output-root",
                str(self.output_root / "missing-candidate"),
            ]
        )
        self.assertEqual(2, missing_code)
        self.assertIsNone(missing_payload)
        self.assertIn("requires --candidate", missing_stderr)

        code, _, stderr, payload = self.invoke(
            [
                "optimize",
                "--pack",
                str(PACK),
                "--subject",
                str(BASELINE),
                "--candidate",
                str(CANDIDATE),
                "--runtime",
                "fake",
                "--output-root",
                str(self.output_root / "optimize"),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["accepted"])
        self.assertTrue(payload["simulated"])
        self.assertEqual("optimization", payload["report_type"])
        self.assert_reports(payload)

        rerun_code, _, rerun_stderr, rerun_payload = self.invoke(
            [
                "run",
                "--pack",
                str(PACK),
                "--subject",
                payload["summary"]["selected_candidate_path"],
                "--runtime",
                "fake",
                "--output-root",
                str(self.output_root / "optimized-candidate-rerun"),
            ]
        )
        self.assertEqual(0, rerun_code, rerun_stderr)
        self.assertEqual("candidate", rerun_payload["summary"]["subject_variant"])

    def test_reference_runtime_requires_model_command(self) -> None:
        code, output, stderr, payload = self.invoke(
            [
                "run",
                "--pack",
                str(PACK),
                "--subject",
                str(BASELINE),
                "--runtime",
                "reference",
                "--output-root",
                str(self.output_root / "reference"),
            ]
        )
        self.assertEqual(2, code)
        self.assertEqual("", output)
        self.assertIsNone(payload)
        self.assertIn("requires --model-command", stderr)

    def test_company_profile_validate_and_local_session_import(self) -> None:
        profile = self.output_root / "company-profile.json"
        profile.write_text(
            json.dumps(
                {
                    "api_version": "aceval.company-profile/v1",
                    "name": "company-agent",
                    "base_url": "https://agent.company.test",
                    "auth": {
                        "type": "bearer_env",
                        "env": "COMPANY_AGENT_TOKEN",
                    },
                    "execute": {"path": "/runs"},
                    "session_log": {
                        "path_template": "/sessions/{session_id}",
                        "output_path": "$.data.output",
                        "trace_path": "$.data.events",
                        "usage_path": "$.data.usage",
                    },
                }
            ),
            encoding="utf-8",
        )
        code, _, stderr, payload = self.invoke(
            ["profile", "validate", str(profile)]
        )
        self.assertEqual(0, code, stderr)
        self.assertEqual("COMPANY_AGENT_TOKEN", payload["auth"]["env"])
        self.assertFalse(payload["secret_loaded"])

        source = self.output_root / "session.json"
        source.write_text(
            json.dumps(
                {
                    "data": {
                        "output": {"ok": True},
                        "events": [
                            {"kind": "tool_call", "name": "read_file"},
                            {
                                "kind": "tool_result",
                                "tool": "process_exec",
                                "payload": {
                                    "ok": False,
                                    "exit_code": 127,
                                    "stderr_excerpt": "command not found",
                                },
                            },
                        ],
                        "usage": {"total_tokens": 10},
                    }
                }
            ),
            encoding="utf-8",
        )
        imported = self.output_root / "imported-session.json"
        code, _, stderr, payload = self.invoke(
            [
                "session",
                "import",
                "--profile",
                str(profile),
                "--session-id",
                "session-1",
                "--input",
                str(source),
                "--output",
                str(imported),
            ]
        )
        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["complete"])
        normalized = json.loads(imported.read_text(encoding="utf-8"))
        self.assertEqual("aceval.imported-session/v1", normalized["schema_version"])
        self.assertEqual(
            "tool_call", normalized["observation"]["trace"][0]["kind"]
        )

        diagnosis = self.output_root / "session-diagnosis.json"
        code, _, stderr, payload = self.invoke(
            [
                "session",
                "diagnose",
                "--input",
                str(imported),
                "--output",
                str(diagnosis),
            ]
        )
        self.assertEqual(0, code, stderr)
        report = payload["diagnostic_report"]
        self.assertEqual("deny_skill_intervention", report["patch_decision"])
        self.assertEqual(
            "cli.binary_not_found",
            report["failure_cards"][0]["reason_code"],
        )
        self.assertTrue(diagnosis.is_file())

    def test_reference_runtime_executes_json_bridge_and_file_tools(self) -> None:
        bridge = self.output_root / "deterministic_bridge.py"
        bridge.write_text(
            """import csv
import io
import json
import sys
from decimal import Decimal, ROUND_HALF_UP

request = json.load(sys.stdin)
messages = request["messages"]
tool_messages = [item for item in messages if item.get("role") == "tool"]
read_messages = [item for item in tool_messages if item.get("name") == "read_file"]
write_messages = [item for item in tool_messages if item.get("name") == "write_file"]

if not read_messages:
    response = {
        "content": "",
        "tool_calls": [
            {"id": "read-1", "name": "read_file", "arguments": {"path": "input.csv"}}
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
else:
    source = json.loads(read_messages[-1]["content"])["result"]
    rows = list(csv.DictReader(io.StringIO(source)))
    totals = {}
    total = Decimal("0")
    for row in rows:
        region = row["region"].strip() or "Unknown"
        amount = Decimal(row["amount"].strip().removeprefix("$"))
        total += amount
        totals[region] = totals.get(region, Decimal("0")) + amount
    quantize = lambda value: float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    summary = {
        "currency": "USD",
        "total_revenue": quantize(total),
        "orders": len(rows),
        "by_region": {key: quantize(totals[key]) for key in sorted(totals)},
    }
    content = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    if not write_messages:
        response = {
            "content": "",
            "tool_calls": [
                {
                    "id": "write-1",
                    "name": "write_file",
                    "arguments": {"path": "summary.json", "content": content},
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    else:
        response = {
            "content": content,
            "tool_calls": [],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

json.dump(response, sys.stdout, separators=(",", ":"))
""",
            encoding="utf-8",
        )
        model_command = shlex.join((sys.executable, str(bridge)))

        code, _, stderr, payload = self.invoke(
            [
                "run",
                "--pack",
                str(PACK),
                "--subject",
                str(CANDIDATE),
                "--split",
                "dev",
                "--runtime",
                "reference",
                "--model-command",
                model_command,
                "--model-id",
                "deterministic-test-model",
                "--model-timeout",
                "10",
                "--max-steps",
                "5",
                "--max-tool-calls",
                "20",
                "--output-root",
                str(self.output_root / "reference-e2e"),
            ]
        )

        self.assertEqual(0, code, stderr)
        self.assertFalse(payload["simulated"])
        self.assertTrue(payload["summary"]["passed"])
        self.assertEqual("reference", payload["summary"]["runtime"])
        self.assertEqual(
            "deterministic-test-model", payload["summary"]["model"]
        )
        self.assertEqual(12, payload["summary"]["total_tokens"])
        self.assert_reports(payload)
        report = json.loads(
            Path(payload["report_json"]).read_text(encoding="utf-8")
        )
        profile = report["runtime_profile"]
        bridge_profile = profile["parameters"]["model_bridge"]
        self.assertEqual(5, profile["parameters"]["config"]["max_steps"])
        self.assertEqual(10.0, bridge_profile["timeout_seconds"])
        self.assertEqual(20, profile["parameters"]["config"]["max_tool_calls"])
        self.assertIn("argv_sha256", bridge_profile)
        self.assertNotIn("argv", bridge_profile)
        self.assertEqual(20, report["configured_budget"]["max_tool_calls"])


if __name__ == "__main__":
    unittest.main()
