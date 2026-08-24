import json
from pathlib import Path
import tempfile
import unittest

from aceval.pack import EvalPackLoader
from aceval.experiments import ExperimentPlan
from aceval.pack_builder import (
    CALIBRATION_CALIBRATING,
    CALIBRATION_DRAFT,
    CALIBRATION_FROZEN,
    PackBuilderError,
    PackCalibrationError,
    begin_calibration,
    calibration_status,
    freeze_evalpack,
    generate_evalpack,
)
from aceval.registry import build_builtin_registry


class PackBuilderTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def read_json(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def tree_bytes(self, root):
        root = Path(root)
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def without_calibration_status(self, manifest):
        value = json.loads(json.dumps(manifest))
        value["metadata"].pop("calibration_status", None)
        return value

    def test_generic_draft_is_deterministic_loadable_and_explicitly_untrusted(self):
        cases = {
            "name": "generic-answer",
            "cases": [
                {
                    "id": "answer-dev",
                    "prompt": "Return a JSON answer object.",
                    "expected_output": {"answer": 42},
                    "tags": ["json"],
                },
                {
                    "id": "answer-validation",
                    "split": "validation",
                    "prompt": "Return another JSON answer object.",
                    "expected_output": {"answer": 7},
                },
            ],
        }
        objective = {
            "id": "token-efficiency",
            "source": {"type": "usage", "key": "total_tokens"},
            "direction": "minimize",
            "aggregation": "mean",
            "min_delta": 1,
            "max_case_regression": 0,
        }

        first = generate_evalpack(
            cases,
            "generic",
            "Keep answers correct while reducing tokens.",
            self.root / "first",
            objective=objective,
        )
        second = generate_evalpack(
            cases,
            "generic",
            "Keep answers correct while reducing tokens.",
            self.root / "second",
            objective=objective,
        )

        self.assertEqual(self.tree_bytes(first.root), self.tree_bytes(second.root))
        self.assertEqual(CALIBRATION_DRAFT, first.calibration_status)
        self.assertFalse(first.trusted)
        manifest = self.read_json(first.manifest_path)
        self.assertEqual("aceval.dev/v1alpha2", manifest["api_version"])
        self.assertEqual("draft", manifest["metadata"]["calibration_status"])
        self.assertNotIn("optimizer_policy", manifest)
        self.assertIsNotNone(first.experiment_path)
        experiment = ExperimentPlan.load(first.experiment_path)
        self.assertEqual("auto", experiment.optimization.mode.value)
        self.assertEqual("token-efficiency", experiment.optimization.objective.id)
        self.assertEqual("total_tokens", experiment.optimization.objective.source.key)
        self.assertEqual(
            ExperimentPlan.load(first.experiment_path).to_dict(),
            ExperimentPlan.load(second.experiment_path).to_dict(),
        )
        self.assertIn("validation_ref", manifest["suite"])
        self.assertTrue((first.root / "fixtures").is_dir())
        self.assertTrue((first.root / "oracles" / "dev" / "answer-dev.json").is_file())

        loaded = EvalPackLoader(build_builtin_registry()).load(first.root)
        self.assertEqual(2, len(loaded.scenarios))
        self.assertEqual(
            CALIBRATION_DRAFT,
            loaded.manifest.metadata.extra["calibration_status"],
        )

    def test_csv_summary_copies_relative_fixture_and_uses_artifact_template(self):
        source = self.root / "source"
        source.mkdir()
        (source / "input.csv").write_text("region,amount\nNorth,10\n", encoding="utf-8")
        cases_path = source / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "id": "summary-dev",
                            "prompt": "Read input.csv and create summary.json.",
                            "fixtures": ["input.csv"],
                            "expected_output": {"total": 10},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = generate_evalpack(
            cases_path,
            "csv-summary",
            "Produce the exact summary without changing the input.",
            self.root / "csv-pack",
        )

        copied = result.root / "fixtures" / "dev" / "summary-dev" / "input.csv"
        self.assertEqual((source / "input.csv").read_bytes(), copied.read_bytes())
        manifest = self.read_json(result.manifest_path)
        self.assertEqual("artifact_workspace", manifest["driver"]["type"])
        self.assertEqual(
            {"artifact_path": "summary.json", "schema_ref": "schemas/summary.schema.json"},
            next(
                grader["params"]
                for grader in manifest["graders"]
                if grader["id"] == "output-schema"
            ),
        )
        scenario = self.read_json(result.root / "scenarios" / "dev.yaml")["scenarios"][0]
        self.assertEqual(["summary.json"], scenario["metadata"]["artifact_paths"])
        self.assertEqual(
            ["input.csv"],
            scenario["grader_params"]["workspace-policy"]["forbidden_paths"],
        )

    def test_security_review_template_generates_records_schema_and_inline_fixture(self):
        result = generate_evalpack(
            {
                "cases": [
                    {
                        "id": "sql-dev",
                        "prompt": "Review app.py and return findings JSON.",
                        "fixtures": [
                            {
                                "path": "app.py",
                                "content": "cursor.execute('SELECT ' + user_input)\n",
                            }
                        ],
                        "expected_records": [
                            {"rule_id": "sql-injection", "file": "app.py", "line": 1}
                        ],
                    }
                ]
            },
            "security-review",
            "Find grounded security vulnerabilities.",
            self.root / "security-pack",
        )

        manifest = self.read_json(result.manifest_path)
        self.assertEqual("repository_workspace", manifest["driver"]["type"])
        self.assertEqual(
            {"json_schema", "record_match", "source_reference", "trace_assert"},
            {grader["type"] for grader in manifest["graders"]},
        )
        oracle = self.read_json(result.root / "oracles" / "dev" / "sql-dev.json")
        self.assertEqual("sql-injection", oracle["expected_records"][0]["rule_id"])
        self.assertEqual(
            "cursor.execute('SELECT ' + user_input)\n",
            (result.root / "fixtures" / "dev" / "sql-dev" / "app.py").read_text(
                encoding="utf-8"
            ),
        )

    def test_freeze_requires_approval_and_changes_only_lifecycle_status(self):
        result = generate_evalpack(
            {
                "cases": [
                    {
                        "id": "ready-dev",
                        "prompt": "Return JSON.",
                        "expected_output": {"ok": True},
                    }
                ]
            },
            "generic",
            "Return the correct result.",
            self.root / "ready-pack",
        )
        before_manifest = self.read_json(result.manifest_path)
        before_files = self.tree_bytes(result.root)

        with self.assertRaises(PackCalibrationError):
            freeze_evalpack(result.root)
        self.assertEqual(CALIBRATION_DRAFT, calibration_status(result.root))

        frozen = freeze_evalpack(result.root, approve=True)
        after_manifest = self.read_json(result.manifest_path)
        after_files = self.tree_bytes(result.root)

        self.assertTrue(frozen.trusted)
        self.assertEqual(CALIBRATION_FROZEN, calibration_status(result.root))
        self.assertNotEqual(result.pack_hash, frozen.pack_hash)
        self.assertEqual(
            self.without_calibration_status(before_manifest),
            self.without_calibration_status(after_manifest),
        )
        self.assertEqual(
            {key: value for key, value in before_files.items() if key != "pack.yaml"},
            {
                key: value
                for key, value in after_files.items()
                if key not in ("pack.yaml", ".aceval-pack-lock.json")
            },
        )
        lock = self.read_json(result.root / ".aceval-pack-lock.json")
        self.assertEqual("aceval.pack-lock/v1", lock["api_version"])
        self.assertEqual(CALIBRATION_FROZEN, lock["calibration_status"])

        again = freeze_evalpack(result.root, approve=True)
        self.assertEqual(frozen.pack_hash, again.pack_hash)

    def test_missing_semantic_oracle_stays_draft_and_cannot_be_frozen(self):
        result = generate_evalpack(
            {"cases": [{"id": "subjective-dev", "prompt": "Write a good answer."}]},
            "generic",
            "Make the answer better.",
            self.root / "subjective-pack",
        )
        oracle = self.read_json(
            result.root / "oracles" / "dev" / "subjective-dev.json"
        )
        self.assertTrue(oracle["calibration_required"])

        with self.assertRaises(PackCalibrationError) as caught:
            freeze_evalpack(result.root, approve=True)

        self.assertIn("lack semantic oracle", str(caught.exception))
        self.assertEqual(CALIBRATION_DRAFT, calibration_status(result.root))

    def test_unknown_type_falls_back_to_generic_and_goal_infers_objective(self):
        result = generate_evalpack(
            {
                "cases": [
                    {
                        "id": "custom-dev",
                        "prompt": "Return JSON.",
                        "expected_output": {"ok": True},
                    }
                ]
            },
            "legal-document-analysis",
            "保持结果正确并减少工具调用次数",
            self.root / "custom-pack",
        )

        manifest = self.read_json(result.manifest_path)
        self.assertEqual("generic", result.pack_type)
        self.assertEqual("legal-document-analysis", result.requested_type)
        self.assertEqual(
            "legal-document-analysis",
            manifest["metadata"]["labels"]["aceval.requested_type"],
        )
        self.assertEqual(
            "legal-document-analysis", manifest["metadata"]["template_fallback"]
        )
        self.assertNotIn("objective_origin", manifest["metadata"])
        self.assertNotIn("optimizer_policy", manifest)
        experiment = ExperimentPlan.load(result.experiment_path)
        self.assertEqual("goal_heuristic", experiment.metadata["objective_origin"])
        self.assertEqual(
            {"type": "trace_count", "key": "tool_call"},
            {
                "type": experiment.optimization.objective.source.type,
                "key": experiment.optimization.objective.source.key,
            },
        )

    def test_experiment_changes_do_not_change_evalpack_hash(self):
        result = generate_evalpack(
            {
                "cases": [
                    {
                        "id": "hash-dev",
                        "prompt": "Return JSON.",
                        "expected_output": {"ok": True},
                    }
                ]
            },
            "generic",
            "Return the correct result.",
            self.root / "hash-pack",
        )
        loader = EvalPackLoader(build_builtin_registry())
        before = loader.load(result.root)
        document = self.read_json(result.experiment_path)
        document["optimization"]["max_rounds"] = 9
        result.experiment_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        after = loader.load(result.root)

        self.assertEqual(before.pack_hash, after.pack_hash)
        self.assertEqual(before.suite_hash, after.suite_hash)
        self.assertEqual(9, ExperimentPlan.load(result.experiment_path).optimization.max_rounds)

    def test_calibration_stage_is_editable_but_frozen_pack_cannot_reenter(self):
        result = generate_evalpack(
            {
                "cases": [
                    {
                        "id": "ready-dev",
                        "prompt": "Return JSON.",
                        "expected_output": {"ok": True},
                    }
                ]
            },
            "generic",
            "Return the correct result.",
            self.root / "calibrating-pack",
        )

        calibrating = begin_calibration(result.root)
        self.assertEqual(CALIBRATION_CALIBRATING, calibrating.calibration_status)
        self.assertEqual(CALIBRATION_CALIBRATING, calibration_status(result.root))
        frozen = freeze_evalpack(result.root, approve=True)
        self.assertTrue(frozen.trusted)
        with self.assertRaisesRegex(PackCalibrationError, "new Pack version"):
            begin_calibration(result.root)

    def test_rejects_unsafe_fixture_and_existing_output(self):
        unsafe = {
            "cases": [
                {
                    "id": "unsafe-dev",
                    "prompt": "Read input.",
                    "fixtures": ["../secret.txt"],
                    "expected_output": {},
                }
            ]
        }
        with self.assertRaises(PackBuilderError):
            generate_evalpack(
                unsafe,
                "generic",
                "Return JSON.",
                self.root / "unsafe-pack",
            )

        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaises(PackBuilderError):
            generate_evalpack(
                {
                    "cases": [
                        {
                            "id": "case-dev",
                            "prompt": "Return JSON.",
                            "expected_output": {},
                        }
                    ]
                },
                "generic",
                "Return JSON.",
                existing,
            )


if __name__ == "__main__":
    unittest.main()
