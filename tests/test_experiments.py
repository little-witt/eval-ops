import json
from pathlib import Path
import tempfile
import unittest

from aceval.experiments import (
    ExperimentPlan,
    ExperimentPlanError,
    legacy_experiment_plan,
    policy_from_mapping,
    resolve_experiment_plan,
    write_experiment_plan,
)
from aceval.pack import EvalPackLoader
from aceval.registry import build_builtin_registry


ROOT = Path(__file__).resolve().parents[1]
LEGACY_PACK = ROOT / "evalpacks" / "csv-summary-smoke"


class ExperimentPlanTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pack = EvalPackLoader(build_builtin_registry()).load(LEGACY_PACK)

    def tearDown(self):
        self.temporary.cleanup()

    def policy(self):
        return policy_from_mapping(
            {
                "adapter": "skill_markdown_v1",
                "patchable_components": ["skill_instruction"],
                "allowed_paths": ["SKILL.md"],
                "visible_splits": ["dev"],
                "max_rounds": 3,
                "mode": "repair",
                "goal": "Repair correctness failures.",
            }
        )

    def test_round_trip_and_suite_binding(self):
        plan = ExperimentPlan(
            name="csv-repair",
            suite_hash=self.pack.suite_hash,
            optimization=self.policy(),
            source="operator",
            metadata={"note": "independent control plane"},
        )
        path = write_experiment_plan(self.root / "experiment.json", plan)
        loaded = ExperimentPlan.load(path)

        self.assertEqual(plan.to_dict(), loaded.to_dict())
        self.assertEqual(plan.content_hash, loaded.content_hash)
        loaded.validate_suite(self.pack)

    def test_rejects_duplicate_fields_and_unsafe_allowed_paths(self):
        duplicate = self.root / "duplicate.json"
        duplicate.write_text(
            '{"api_version":"aceval.experiment-plan/v1",'
            '"api_version":"aceval.experiment-plan/v1"}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ExperimentPlanError, "strict UTF-8 JSON"):
            ExperimentPlan.load(duplicate)

        with self.assertRaisesRegex(ExperimentPlanError, "Subject-relative"):
            policy_from_mapping(
                {
                    "adapter": "skill_markdown_v1",
                    "allowed_paths": ["../SKILL.md"],
                }
            )

    def test_rejects_plan_for_a_different_eval_suite(self):
        plan = ExperimentPlan(
            name="wrong-suite",
            suite_hash="0" * 64,
            optimization=self.policy(),
        )
        with self.assertRaisesRegex(ExperimentPlanError, "does not match"):
            plan.validate_suite(self.pack)

    def test_programmatic_metadata_cannot_override_contract_fields(self):
        with self.assertRaisesRegex(ExperimentPlanError, "cannot override"):
            ExperimentPlan(
                name="safe-name",
                suite_hash=self.pack.suite_hash,
                optimization=self.policy(),
                metadata={"name": "shadowed"},
            )

    def test_legacy_optimizer_policy_is_adapted_without_migration(self):
        plan = legacy_experiment_plan(self.pack)

        self.assertEqual("legacy_pack", plan.source)
        self.assertEqual(self.pack.suite_hash, plan.suite_hash)
        self.assertEqual(
            self.pack.manifest.optimizer_policy,
            plan.optimization,
        )
        self.assertEqual(
            plan.to_dict(),
            resolve_experiment_plan(self.pack).to_dict(),
        )

    def test_explicit_plan_takes_precedence_and_must_match(self):
        path = self.root / "explicit.json"
        document = {
            "api_version": "aceval.experiment-plan/v1",
            "kind": "ExperimentPlan",
            "metadata": {"name": "explicit", "source": "test"},
            "eval_suite": {"suite_hash": self.pack.suite_hash},
            "optimization": {
                "adapter": "skill_markdown_v1",
                "patchable_components": ["skill_instruction"],
                "allowed_paths": ["SKILL.md"],
                "visible_splits": ["dev"],
                "mode": "repair",
            },
        }
        path.write_text(json.dumps(document), encoding="utf-8")

        resolved = resolve_experiment_plan(self.pack, path)
        self.assertEqual("explicit", resolved.name)
        self.assertEqual("repair", resolved.optimization.mode.value)


if __name__ == "__main__":
    unittest.main()
