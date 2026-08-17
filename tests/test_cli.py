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
