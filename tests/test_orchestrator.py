import difflib
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from aceval.contracts import (
    CANDIDATE_PATCH_OPTIMIZER_CONTRACT,
    CandidatePatch,
    GradeResult,
    GradeStatus,
    RunBudget,
    RuntimeProfile,
    RuntimeResult,
    SubjectSnapshot,
    TraceEvent,
)
from aceval.drivers import ArtifactWorkspaceDriver, builtin_drivers
from aceval.graders import builtin_graders
from aceval.optimizer import (
    CandidateRejected,
    FrozenCandidateOptimizer,
    SkillOptimizerBridge,
    TuneEvidence,
)
from aceval.orchestrator import (
    EvalOrchestrator,
    OrchestrationError,
    ScenarioEvaluation,
    _BudgetLedger,
)
from aceval.pack import ComponentRegistry, EvalPackLoader
from aceval.pack_builder import generate_evalpack
from aceval.pack_lifecycle import write_pack_lock
from aceval.reporting import to_report_dict
from aceval.runtime import FakeRuntime
from aceval.subjects import SkillMarkdownSubjectAdapter


ROOT = Path(__file__).resolve().parents[1]


def component_registry():
    registry = ComponentRegistry()
    registry.register_subject_adapter(
        "skill_markdown_v1", SkillMarkdownSubjectAdapter(), kinds=("skill",)
    )
    for component_id, driver in builtin_drivers().items():
        registry.register_driver(component_id, driver)
    for component_id, grader in builtin_graders().items():
        registry.register_grader(component_id, grader)
    registry.register_optimizer("skill_markdown_v1", FrozenCandidateOptimizer)
    return registry


class RecordingCandidateOptimizer(FrozenCandidateOptimizer):
    def __init__(self, candidate):
        super().__init__(candidate)
        self.calls = []

    def propose(self, subject, failures, output_root, policy=None, forbidden_literals=()):
        self.calls.append(tuple(failures))
        return super().propose(
            subject,
            failures,
            output_root,
            policy=policy,
            forbidden_literals=forbidden_literals,
        )


class ContextCapturingRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.contexts = []

    async def execute(self, prepared, subject, context=None):
        self.contexts.append(context)
        return await super().execute(prepared, subject, context)


class ParamCapturingSubjectAdapter(SkillMarkdownSubjectAdapter):
    def __init__(self):
        self.snapshot_params = []
        self.materialize_params = []

    def snapshot(self, subject_ref, params=None):
        self.snapshot_params.append(dict(params or {}))
        return super().snapshot(subject_ref, params)

    def materialize(self, snapshot, destination, params=None):
        self.materialize_params.append(dict(params or {}))
        return super().materialize(snapshot, destination, params)


class ParamCapturingDriver(ArtifactWorkspaceDriver):
    def __init__(self):
        super().__init__()
        self.params = []

    async def prepare(self, scenario, context):
        self.params.append(dict(context.metadata.get("driver_params", {})))
        return await super().prepare(scenario, context)


class ForgedPatchBridge(SkillOptimizerBridge):
    id = "skill_markdown_v1"

    def __init__(self, mode, output_root, candidate_source):
        self.mode = mode
        self.output_root = Path(output_root)
        self.candidate_source = Path(candidate_source)

    async def propose(self, base, diagnoses, constraints):
        del diagnoses
        candidate_root = next(self.output_root.glob("optimize-*/candidates"))
        candidate_path = self.candidate_source
        expected_hash = None
        if self.mode in ("hash", "diff"):
            candidate_path = candidate_root / "forged"
            candidate_path.mkdir()
            (candidate_path / "SKILL.md").write_text(
                (self.candidate_source / "SKILL.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            if self.mode == "hash":
                expected_hash = "0" * 64

        unified_diff = (
            "--- a/SKILL.md\n"
            "+++ b/SKILL.md\n"
            "@@ -1 +1 @@\n"
            "-baseline\n"
            "+candidate\n"
        )
        metadata = {"path": str(candidate_path)}
        if expected_hash is not None:
            metadata["subject_hash"] = expected_hash
        return (
            CandidatePatch(
                base_hash="0" * 64 if self.mode == "base" else base.content_hash,
                unified_diff=unified_diff,
                allowed_paths=tuple(constraints.allowed_paths),
                patch_hash=hashlib.sha256(unified_diff.encode("utf-8")).hexdigest(),
                metadata=metadata,
            ),
        )


class GenericPatchOptimizer:
    id = "skill_markdown_v1"
    proposal_contract = CANDIDATE_PATCH_OPTIMIZER_CONTRACT

    def __init__(self, candidate_source):
        self.candidate_source = Path(candidate_source)
        self.calls = []

    async def propose(self, base, diagnoses, constraints):
        self.calls.append((base, tuple(diagnoses), constraints))
        candidate_root = Path(constraints.metadata["candidate_root"])
        candidate_path = candidate_root / "generic-candidate"
        candidate_path.mkdir(exist_ok=True)
        candidate_text = (self.candidate_source / "SKILL.md").read_text(
            encoding="utf-8"
        )
        (candidate_path / "SKILL.md").write_text(candidate_text, encoding="utf-8")
        for relative, content in base.files.items():
            if relative == "SKILL.md":
                continue
            target = candidate_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        diff = "\n".join(
            difflib.unified_diff(
                str(base.content).splitlines(),
                candidate_text.splitlines(),
                fromfile="a/SKILL.md",
                tofile="b/SKILL.md",
                lineterm="",
            )
        ) + "\n"
        return (
            CandidatePatch(
                base_hash=base.content_hash,
                unified_diff=diff,
                allowed_paths=tuple(constraints.allowed_paths),
                patch_hash=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                metadata={"path": str(candidate_path), "snapshot_id": "generic"},
            ),
        )


class SequencedGenericOptimizer:
    id = "skill_markdown_v1"
    proposal_contract = CANDIDATE_PATCH_OPTIMIZER_CONTRACT

    def __init__(self, candidate_texts, shared_id=False):
        self.candidate_texts = tuple(candidate_texts)
        self.shared_id = shared_id
        self.calls = 0
        self.hashes = []

    async def propose(self, base, diagnoses, constraints):
        del diagnoses
        index = self.calls
        self.calls += 1
        candidate_text = self.candidate_texts[index]
        candidate_root = Path(constraints.metadata["candidate_root"])
        candidate_path = candidate_root / ("sequenced-%s" % index)
        candidate_path.mkdir()
        for relative, content in base.files.items():
            target = candidate_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                candidate_text.encode("utf-8")
                if relative == "SKILL.md"
                else content
            )
        diff = "\n".join(
            difflib.unified_diff(
                str(base.content).splitlines(),
                candidate_text.splitlines(),
                fromfile="a/SKILL.md",
                tofile="b/SKILL.md",
                lineterm="",
            )
        ) + "\n"
        snapshot = SkillMarkdownSubjectAdapter().snapshot(str(candidate_path))
        self.hashes.append(snapshot.content_hash)
        return (
            CandidatePatch(
                base_hash=base.content_hash,
                unified_diff=diff,
                allowed_paths=tuple(constraints.allowed_paths),
                patch_hash=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                metadata={
                    "path": str(candidate_path),
                    "snapshot_id": (
                        "shared-id" if self.shared_id else "candidate-%s" % index
                    ),
                    "subject_hash": snapshot.content_hash,
                },
            ),
        )


class UsageTuningRuntime(FakeRuntime):
    """Use passing declarative outputs while varying paired usage telemetry."""

    def __init__(
        self,
        baseline_hash,
        *,
        baseline_tokens=100,
        candidate_tokens=50,
        token_overrides=None,
        hard_failures=(),
        missing_usage=(),
    ):
        super().__init__()
        self.baseline_hash = baseline_hash
        self.baseline_tokens = baseline_tokens
        self.candidate_tokens = candidate_tokens
        self.token_overrides = dict(token_overrides or {})
        self.hard_failures = frozenset(hard_failures)
        self.missing_usage = frozenset(missing_usage)

    async def execute(self, prepared, subject, context=None):
        result = await super().execute(prepared, subject, context)
        scenario_id = prepared.metadata.get("scenario_id")
        side = (
            "baseline"
            if subject.content_hash == self.baseline_hash
            else "candidate"
        )
        self.calls[-1]["tune_side"] = side
        key = (scenario_id, side)
        if key in self.hard_failures:
            result = replace(result, final_output={"findings": []})
        if key in self.missing_usage:
            return replace(result, usage={})
        tokens = self.token_overrides.get(
            key,
            self.baseline_tokens if side == "baseline" else self.candidate_tokens,
        )
        return replace(result, usage={"total_tokens": tokens})


class OrchestratorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name) / "runs"

    def tearDown(self):
        self.temporary.cleanup()

    def load_pack(self, name, registry):
        return EvalPackLoader(registry).load(ROOT / "evalpacks" / name)

    def subject(self, name):
        return ROOT / "examples" / "subjects" / name

    def tune_pack(self, registry):
        pack_root = Path(self.temporary.name) / "tune-pack"
        shutil.copytree(ROOT / "evalpacks" / "security-review", pack_root)
        manifest_path = pack_root / "pack.yaml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["api_version"] = "aceval.dev/v1alpha2"
        manifest["metadata"]["calibration_status"] = "frozen"
        manifest["optimizer_policy"].update(
            {
                "mode": "tune",
                "goal": "Preserve review correctness while reducing tokens.",
                "objective": {
                    "id": "token-efficiency",
                    "source": {"type": "usage", "key": "total_tokens"},
                    "direction": "minimize",
                    "aggregation": "mean",
                    "min_delta": 10,
                    "max_case_regression": 0,
                },
                "beam_width": 1,
                "max_rounds": 1,
                "max_candidate_snapshots": 1,
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        write_pack_lock(pack_root, manifest["metadata"]["version"])
        return EvalPackLoader(registry).load(pack_root)

    def tune_subjects(self):
        baseline = self.subject("security-review-skill-candidate")
        candidate = Path(self.temporary.name) / "tuned-security-review-skill"
        shutil.copytree(baseline, candidate)
        skill_path = candidate / "SKILL.md"
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8")
            + "\nPrefer the shortest sufficient tool sequence.\n",
            encoding="utf-8",
        )
        return baseline, candidate

    def test_soft_grader_errors_never_count_as_hard_passes(self):
        for status in (GradeStatus.ERROR, GradeStatus.NOT_EVALUABLE):
            with self.subTest(status=status):
                scenario = ScenarioEvaluation(
                    scenario_id="soft-grader",
                    split="dev",
                    status=status,
                    grades=(
                        GradeResult(
                            grader_id="advisory",
                            status=status,
                            hard=False,
                        ),
                    ),
                )

                self.assertFalse(scenario.hard_passed)

    async def test_evaluate_and_compare_reject_holdout(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        runtime = FakeRuntime()
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        )

        with self.assertRaises(OrchestrationError):
            await orchestrator.evaluate(
                pack, self.subject("security-review-skill"), ("holdout",)
            )
        with self.assertRaises(OrchestrationError):
            await orchestrator.compare(
                pack,
                self.subject("security-review-skill"),
                self.subject("security-review-skill-candidate"),
                ("holdout",),
            )

        self.assertEqual([], runtime.calls)

    async def test_optimize_rejects_generated_draft_before_runtime_execution(self):
        registry = component_registry()
        generated = generate_evalpack(
            {
                "cases": [
                    {
                        "id": "draft-dev",
                        "prompt": "Return JSON.",
                        "expected_output": {"ok": True},
                    }
                ]
            },
            "generic",
            "Return the correct result.",
            self.output_root / "draft-pack",
            registry=registry,
        )
        pack = EvalPackLoader(registry).load(generated.root)
        runtime = FakeRuntime()
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root / "draft-runs"
        )

        with self.assertRaisesRegex(OrchestrationError, "explicitly freeze"):
            await orchestrator.optimize(
                pack,
                self.subject("csv-summary-skill"),
                FrozenCandidateOptimizer(
                    self.subject("csv-summary-skill-candidate")
                ),
            )

        self.assertEqual([], runtime.calls)

    async def test_compare_baseline_and_candidate_on_both_real_packs(self):
        cases = (
            (
                "csv-summary-smoke",
                "csv-summary-skill",
                "csv-summary-skill-candidate",
                {"dev", "validation"},
            ),
            (
                "security-review",
                "security-review-skill",
                "security-review-skill-candidate",
                {"dev", "validation"},
            ),
        )
        for pack_name, baseline_name, candidate_name, splits in cases:
            with self.subTest(pack=pack_name):
                registry = component_registry()
                pack = self.load_pack(pack_name, registry)
                orchestrator = EvalOrchestrator(
                    registry,
                    FakeRuntime(),
                    output_root=self.output_root / pack_name,
                )

                comparison = await orchestrator.compare(
                    pack,
                    self.subject(baseline_name),
                    self.subject(candidate_name),
                    splits,
                )

                self.assertFalse(comparison.baseline.passed)
                self.assertTrue(comparison.candidate.passed)
                self.assertGreater(comparison.paired_uplift, 0)
                self.assertTrue(comparison.improvements)
                self.assertEqual((), comparison.hard_regressions)
                self.assertTrue(comparison.accepted)
                expected = {
                    scenario.id for scenario in pack.scenarios if scenario.split in splits
                }
                self.assertEqual(
                    expected,
                    {item.scenario_id for item in comparison.candidate.scenarios},
                )

    async def test_compare_freezes_one_shot_split_iterables(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        orchestrator = EvalOrchestrator(
            registry, FakeRuntime(), output_root=self.output_root
        )

        comparison = await orchestrator.compare(
            pack,
            self.subject("csv-summary-skill"),
            self.subject("csv-summary-skill-candidate"),
            (split for split in ("dev", "validation")),
        )

        self.assertEqual(
            comparison.baseline.requested_splits,
            comparison.candidate.requested_splits,
        )
        self.assertEqual(("dev", "validation"), comparison.candidate.requested_splits)

    async def test_compare_is_not_evaluable_when_baseline_has_infrastructure_error(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        runtime = FakeRuntime(
            {
                ("currency-values-dev", "baseline"): RuntimeResult(
                    error="synthetic baseline infrastructure failure"
                )
            }
        )
        comparison = await EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        ).compare(
            pack,
            self.subject("csv-summary-skill"),
            self.subject("csv-summary-skill-candidate"),
            ("dev",),
        )

        self.assertFalse(comparison.evaluable)
        self.assertFalse(comparison.accepted)
        self.assertEqual("error", comparison.status)
        self.assertIsNone(comparison.paired_uplift)
        self.assertEqual((), comparison.improvements)
        self.assertEqual((), comparison.hard_regressions)

    async def test_optimize_preserves_source_and_passes_validation_and_holdout(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        source = self.subject("security-review-skill")
        candidate = self.subject("security-review-skill-candidate")
        original = (source / "SKILL.md").read_bytes()
        orchestrator = EvalOrchestrator(
            registry, FakeRuntime(), output_root=self.output_root
        )

        result = await orchestrator.optimize(
            pack, source, FrozenCandidateOptimizer(candidate)
        )

        self.assertTrue(result.accepted)
        self.assertFalse(result.baseline_dev.passed)
        self.assertEqual(1, len(result.trials))
        self.assertTrue(result.trials[0].promoted_from_dev)
        self.assertTrue(result.trials[0].dev_run.passed)
        self.assertIsNotNone(result.baseline_validation)
        self.assertIsNotNone(result.candidate_validation)
        self.assertTrue(result.candidate_validation.passed)
        self.assertEqual(1, len(result.validation_attempts))
        self.assertTrue(result.validation_attempts[0].passed_gate)
        self.assertIsNotNone(result.candidate_holdout)
        self.assertTrue(result.candidate_holdout.passed)
        self.assertEqual(1, result.holdout_batch_count)
        self.assertIsNotNone(result.selected_candidate_path)
        self.assertEqual(
            result.selected_candidate_hash,
            SkillMarkdownSubjectAdapter()
            .snapshot(str(result.selected_candidate_path))
            .content_hash,
        )

        self.assertEqual("repair", result.mode)
        self.assertIsNone(result.objective)
        self.assertIsNone(result.baseline_holdout)
        self.assertEqual(0, result.holdout_pair_count)
        self.assertNotEqual(source.resolve(), result.selected_candidate_path.resolve())
        self.assertEqual(original, (source / "SKILL.md").read_bytes())
        self.assertEqual(
            (candidate / "SKILL.md").read_text(encoding="utf-8"),
            (result.selected_candidate_path / "SKILL.md").read_text(encoding="utf-8"),
        )
        delivered_before = (result.selected_candidate_path / "SKILL.md").read_bytes()
        delivered_hash_before = result.selected_candidate_hash
        trial_skill = result.trials[0].candidate_path / "SKILL.md"
        trial_skill.chmod(0o644)
        trial_skill.write_text("mutated after selection", encoding="utf-8")
        self.assertEqual(
            delivered_before,
            (result.selected_candidate_path / "SKILL.md").read_bytes(),
        )
        self.assertEqual(delivered_hash_before, result.selected_candidate_hash)
        self.assertEqual(
            result.selected_candidate_hash,
            SkillMarkdownSubjectAdapter()
            .snapshot(str(result.selected_candidate_path))
            .content_hash,
        )

    async def test_tune_reduces_tokens_through_paired_dev_validation_and_holdout(self):
        registry = component_registry()
        pack = self.tune_pack(registry)
        baseline, candidate = self.tune_subjects()
        baseline_hash = SkillMarkdownSubjectAdapter().snapshot(
            str(baseline)
        ).content_hash
        runtime = UsageTuningRuntime(baseline_hash)
        optimizer = RecordingCandidateOptimizer(candidate)

        result = await EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        ).optimize(pack, baseline, optimizer)

        self.assertTrue(result.accepted)
        self.assertEqual("tune", result.mode)
        self.assertTrue(result.baseline_dev.passed)
        self.assertEqual("token-efficiency", result.objective.id)
        self.assertEqual(1, len(result.trials))
        self.assertTrue(result.trials[0].promoted_from_dev)
        self.assertEqual(50.0, result.dev_objective.improvement)
        self.assertTrue(result.dev_objective.passed)
        self.assertTrue(result.validation_objective.passed)
        self.assertTrue(result.holdout_objective.passed)
        self.assertEqual(1, result.holdout_batch_count)
        self.assertEqual(1, result.holdout_pair_count)
        self.assertIsNotNone(result.baseline_holdout)
        self.assertIsNotNone(result.candidate_holdout)
        self.assertEqual(1, len(optimizer.calls))
        evidence = optimizer.calls[0]
        self.assertEqual(1, len(evidence))
        self.assertIsInstance(evidence[0], TuneEvidence)
        self.assertEqual(
            "Preserve review correctness while reducing tokens.", evidence[0].goal
        )
        self.assertEqual(
            {
                scenario.id
                for scenario in pack.scenarios
                if scenario.split == "dev"
            },
            set(evidence[0].scenario_values),
        )
        holdout_calls = [
            call
            for call in runtime.calls
            if call["scenario_id"].endswith("-holdout")
        ]
        self.assertEqual(
            {"baseline", "candidate"},
            {call["tune_side"] for call in holdout_calls},
        )
        report = to_report_dict(result)["summary"]
        self.assertEqual(1, report["holdout_pair_count"])
        self.assertTrue(report["holdout_objective"]["passed"])

    async def test_auto_selects_tune_for_a_passing_measured_baseline(self):
        registry = component_registry()
        pack = self.tune_pack(registry)
        baseline, candidate = self.tune_subjects()
        baseline_hash = SkillMarkdownSubjectAdapter().snapshot(
            str(baseline)
        ).content_hash

        result = await EvalOrchestrator(
            registry,
            UsageTuningRuntime(baseline_hash),
            output_root=self.output_root,
        ).optimize(
            pack,
            baseline,
            FrozenCandidateOptimizer(candidate),
            mode="auto",
        )

        self.assertEqual("tune", result.mode)
        self.assertTrue(result.accepted)
        self.assertIsNotNone(result.dev_objective)

    async def test_auto_selects_repair_for_a_failing_baseline(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)

        result = await EvalOrchestrator(
            registry, FakeRuntime(), output_root=self.output_root
        ).optimize(
            pack,
            self.subject("security-review-skill"),
            FrozenCandidateOptimizer(
                self.subject("security-review-skill-candidate")
            ),
            mode="auto",
        )

        self.assertEqual("repair", result.mode)
        self.assertTrue(result.accepted)
        self.assertIsNone(result.objective)

    async def test_tune_rejects_hard_regression_even_when_tokens_improve(self):
        registry = component_registry()
        pack = self.tune_pack(registry)
        baseline, candidate = self.tune_subjects()
        baseline_hash = SkillMarkdownSubjectAdapter().snapshot(
            str(baseline)
        ).content_hash
        runtime = UsageTuningRuntime(
            baseline_hash,
            hard_failures={("command-injection-dev", "candidate")},
        )

        result = await EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        ).optimize(pack, baseline, FrozenCandidateOptimizer(candidate))

        self.assertFalse(result.accepted)
        self.assertEqual(1, len(result.trials))
        trial = result.trials[0]
        self.assertFalse(trial.promoted_from_dev)
        self.assertEqual(50.0, trial.objective_comparison.improvement)
        self.assertIn("hard gate", trial.rejection_reason)
        self.assertIsNone(result.baseline_validation)
        self.assertFalse(
            any(
                call["scenario_id"].endswith("-validation")
                for call in runtime.calls
            )
        )

    async def test_tune_missing_baseline_metric_stops_before_optimizer(self):
        registry = component_registry()
        pack = self.tune_pack(registry)
        baseline, candidate = self.tune_subjects()
        baseline_hash = SkillMarkdownSubjectAdapter().snapshot(
            str(baseline)
        ).content_hash
        runtime = UsageTuningRuntime(
            baseline_hash,
            missing_usage={("command-injection-dev", "baseline")},
        )
        optimizer = RecordingCandidateOptimizer(candidate)

        result = await EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        ).optimize(pack, baseline, optimizer)

        self.assertFalse(result.accepted)
        self.assertEqual((), result.trials)
        self.assertEqual([], optimizer.calls)
        self.assertIn("not completely measurable", result.stop_reason)
        self.assertIn(
            "Missing objective values are not interpreted as zero.",
            result.limitations,
        )

    async def test_tune_requires_validation_objective_improvement(self):
        registry = component_registry()
        pack = self.tune_pack(registry)
        baseline, candidate = self.tune_subjects()
        baseline_hash = SkillMarkdownSubjectAdapter().snapshot(
            str(baseline)
        ).content_hash
        runtime = UsageTuningRuntime(
            baseline_hash,
            token_overrides={
                ("allowlisted-command-validation", "candidate"): 100,
                ("decoded-path-validation", "candidate"): 100,
            },
        )

        result = await EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        ).optimize(pack, baseline, FrozenCandidateOptimizer(candidate))

        self.assertFalse(result.accepted)
        self.assertEqual(1, len(result.validation_attempts))
        attempt = result.validation_attempts[0]
        self.assertFalse(attempt.passed_gate)
        self.assertTrue(attempt.objective_comparison.evaluable)
        self.assertEqual(0.0, attempt.objective_comparison.improvement)
        self.assertFalse(attempt.objective_comparison.passed)
        self.assertIsNone(result.candidate_holdout)
        self.assertEqual(0, result.holdout_pair_count)
        report = to_report_dict(result)["summary"]
        validation_gate = next(
            gate for gate in report["gates"] if gate["gate"] == "validation"
        )
        self.assertEqual("fail", validation_gate["status"])
        self.assertFalse(validation_gate["objective"]["passed"])
        self.assertEqual("fail", report["validation_attempts"][0]["status"])

    async def test_tune_stops_when_baseline_validation_has_hard_failure(self):
        registry = component_registry()
        pack = self.tune_pack(registry)
        baseline, candidate = self.tune_subjects()
        baseline_hash = SkillMarkdownSubjectAdapter().snapshot(
            str(baseline)
        ).content_hash
        runtime = UsageTuningRuntime(
            baseline_hash,
            hard_failures={("decoded-path-validation", "baseline")},
        )

        result = await EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        ).optimize(pack, baseline, FrozenCandidateOptimizer(candidate))

        self.assertFalse(result.accepted)
        self.assertIsNotNone(result.baseline_validation)
        self.assertFalse(result.baseline_validation.passed)
        self.assertIsNone(result.candidate_validation)
        self.assertIn("baseline is not tune-eligible", result.stop_reason)
        self.assertFalse(
            any(
                call["tune_side"] == "candidate"
                and call["scenario_id"].endswith("-validation")
                for call in runtime.calls
            )
        )
        validation_gate = next(
            gate
            for gate in to_report_dict(result)["summary"]["gates"]
            if gate["gate"] == "validation"
        )
        self.assertEqual("fail", validation_gate["status"])

    async def test_tune_stops_when_baseline_holdout_has_hard_failure(self):
        registry = component_registry()
        pack = self.tune_pack(registry)
        baseline, candidate = self.tune_subjects()
        baseline_hash = SkillMarkdownSubjectAdapter().snapshot(
            str(baseline)
        ).content_hash
        runtime = UsageTuningRuntime(
            baseline_hash,
            hard_failures={("popen-command-holdout", "baseline")},
        )

        result = await EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        ).optimize(pack, baseline, FrozenCandidateOptimizer(candidate))

        self.assertFalse(result.accepted)
        self.assertIsNotNone(result.baseline_holdout)
        self.assertFalse(result.baseline_holdout.passed)
        self.assertIsNone(result.candidate_holdout)
        self.assertEqual(0, result.holdout_batch_count)
        self.assertEqual(0, result.holdout_pair_count)
        self.assertIn("baseline is not tune-eligible", result.stop_reason)
        self.assertFalse(
            any(
                call["tune_side"] == "candidate"
                and call["scenario_id"].endswith("-holdout")
                for call in runtime.calls
            )
        )
        holdout_gate = next(
            gate
            for gate in to_report_dict(result)["summary"]["gates"]
            if gate["gate"] == "holdout"
        )
        self.assertEqual("fail", holdout_gate["status"])

    async def test_tune_missing_candidate_holdout_metric_fails_closed(self):
        registry = component_registry()
        pack = self.tune_pack(registry)
        baseline, candidate = self.tune_subjects()
        baseline_hash = SkillMarkdownSubjectAdapter().snapshot(
            str(baseline)
        ).content_hash
        runtime = UsageTuningRuntime(
            baseline_hash,
            missing_usage={("popen-command-holdout", "candidate")},
        )

        result = await EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        ).optimize(pack, baseline, FrozenCandidateOptimizer(candidate))

        self.assertFalse(result.accepted)
        self.assertEqual(1, result.holdout_batch_count)
        self.assertEqual(1, result.holdout_pair_count)
        self.assertFalse(result.holdout_objective.evaluable)
        self.assertFalse(result.holdout_objective.passed)
        self.assertIn("paired holdout tune gate", result.stop_reason)

    async def test_generic_candidate_patch_optimizer_runs_without_skill_bridge(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        optimizer = GenericPatchOptimizer(
            self.subject("security-review-skill-candidate")
        )
        orchestrator = EvalOrchestrator(
            registry, FakeRuntime(), output_root=self.output_root
        )

        result = await orchestrator.optimize(
            pack, self.subject("security-review-skill"), optimizer
        )

        self.assertTrue(result.accepted)
        self.assertEqual(pack.manifest.optimizer_policy.beam_width, len(optimizer.calls))
        constraints = optimizer.calls[0][2]
        self.assertEqual({}, constraints.metadata["optimizer_params"])
        self.assertTrue(Path(constraints.metadata["candidate_root"]).is_dir())

    def test_generic_candidate_patch_requires_complete_text_snapshots(self):
        parent = SubjectSnapshot(
            kind="opaque",
            uri="opaque:parent",
            content_hash="parent-hash",
            content="baseline",
            files={"SKILL.md": b"baseline"},
        )
        diff = (
            "--- a/SKILL.md\n"
            "+++ b/SKILL.md\n"
            "@@ -1 +1 @@\n"
            "-baseline\n"
            "+candidate\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate_path = root / "candidate"
            candidate_path.mkdir()
            patch = CandidatePatch(
                base_hash=parent.content_hash,
                unified_diff=diff,
                allowed_paths=("SKILL.md",),
                patch_hash=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                metadata={"path": str(candidate_path)},
            )

            class StaticAdapter:
                def __init__(self, snapshot):
                    self._snapshot = snapshot

                def snapshot(self, subject_ref, params=None):
                    del subject_ref, params
                    return self._snapshot

            cases = (
                (
                    "opaque-files",
                    SubjectSnapshot(
                        kind="opaque",
                        uri=str(candidate_path),
                        content_hash="candidate-hash",
                        content="candidate",
                        files={},
                    ),
                    "complete parent and candidate file snapshots",
                ),
                (
                    "binary-content",
                    SubjectSnapshot(
                        kind="binary",
                        uri=str(candidate_path),
                        content_hash="candidate-hash",
                        content=b"candidate",
                        files={"SKILL.md": b"candidate"},
                    ),
                    "verifiable text content",
                ),
            )
            for label, snapshot, message in cases:
                with self.subTest(adapter=label):
                    with self.assertRaisesRegex(OrchestrationError, message):
                        EvalOrchestrator._validate_candidate_patch(
                            patch,
                            parent,
                            root,
                            StaticAdapter(snapshot),
                            ("SKILL.md",),
                            30,
                            (),
                            "SKILL.md",
                            {},
                        )

    async def test_optimizer_cannot_reuse_id_for_different_candidate_hashes(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        candidate_text = (
            self.subject("security-review-skill-candidate") / "SKILL.md"
        ).read_text(encoding="utf-8")
        optimizer = SequencedGenericOptimizer(
            (candidate_text, candidate_text + "\nPrefer concise evidence.\n"),
            shared_id=True,
        )

        with self.assertRaisesRegex(OrchestrationError, "reused a candidate id"):
            await EvalOrchestrator(
                registry, FakeRuntime(), output_root=self.output_root
            ).optimize(
                pack,
                self.subject("security-review-skill"),
                optimizer,
            )

    async def test_all_validation_attempts_are_audited_before_selection(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        candidate_text = (
            self.subject("security-review-skill-candidate") / "SKILL.md"
        ).read_text(encoding="utf-8")
        optimizer = SequencedGenericOptimizer(
            (candidate_text, candidate_text + "\nPrefer concise evidence.\n")
        )

        class FirstCandidateFailsValidation(FakeRuntime):
            async def execute(self, prepared, subject, context=None):
                scenario_id = prepared.metadata.get("scenario_id")
                if (
                    scenario_id == "decoded-path-validation"
                    and optimizer.hashes
                    and subject.content_hash == optimizer.hashes[0]
                ):
                    return RuntimeResult(
                        final_output={"findings": []},
                        trace=(
                            TraceEvent(
                                kind="tool_call",
                                name="read_file",
                                payload={"path": "decoded_path.py"},
                            ),
                        ),
                    )
                return await super().execute(prepared, subject, context)

        result = await EvalOrchestrator(
            registry,
            FirstCandidateFailsValidation(),
            output_root=self.output_root,
        ).optimize(
            pack,
            self.subject("security-review-skill"),
            optimizer,
        )

        self.assertTrue(result.accepted)
        self.assertEqual(2, len(result.validation_attempts))
        self.assertFalse(result.validation_attempts[0].passed_gate)
        self.assertTrue(result.validation_attempts[1].passed_gate)
        self.assertEqual(
            result.validation_attempts[1].candidate_id,
            result.selected_candidate_id,
        )
        self.assertEqual(
            optimizer.hashes[1], result.validation_attempts[1].candidate_hash
        )
        validation_gate = next(
            gate
            for gate in to_report_dict(result)["summary"]["gates"]
            if gate["gate"] == "validation"
        )
        self.assertEqual("pass", validation_gate["status"])

    async def test_optimize_stops_at_validation_gate(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        runtime = FakeRuntime(
            {
                ("decoded-path-validation", "candidate"): {
                    "final_output": {"findings": []},
                    "trace": [
                        {
                            "kind": "tool_call",
                            "name": "read_file",
                            "payload": {"path": "decoded_path.py"},
                        }
                    ],
                }
            }
        )
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        )

        result = await orchestrator.optimize(
            pack,
            self.subject("security-review-skill"),
            FrozenCandidateOptimizer(
                self.subject("security-review-skill-candidate")
            ),
        )

        self.assertFalse(result.accepted)
        self.assertIsNotNone(result.candidate_validation)
        self.assertFalse(result.candidate_validation.passed)
        self.assertIsNone(result.selected_candidate_id)
        self.assertIsNone(result.candidate_holdout)
        self.assertEqual(0, result.holdout_batch_count)
        self.assertIn("validation gate", result.stop_reason)
        self.assertFalse(
            any(
                call["subject_variant"] == "candidate"
                and call["scenario_id"].endswith("-holdout")
                for call in runtime.calls
            )
        )

    async def test_optimize_rejects_candidate_at_final_holdout_gate(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        runtime = FakeRuntime(
            {
                ("popen-command-holdout", "candidate"): {
                    "final_output": {"findings": []},
                    "trace": [
                        {
                            "kind": "tool_call",
                            "name": "read_file",
                            "payload": {"path": "popen_command.py"},
                        }
                    ],
                }
            }
        )
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        )

        result = await orchestrator.optimize(
            pack,
            self.subject("security-review-skill"),
            FrozenCandidateOptimizer(
                self.subject("security-review-skill-candidate")
            ),
        )

        self.assertFalse(result.accepted)
        self.assertIsNotNone(result.selected_candidate_id)
        self.assertTrue(result.candidate_validation.passed)
        self.assertIsNotNone(result.candidate_holdout)
        self.assertFalse(result.candidate_holdout.passed)
        self.assertEqual(1, result.holdout_batch_count)
        self.assertIn("holdout gate", result.stop_reason)

    async def test_validation_infrastructure_failure_stops_the_experiment(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        subject = self.subject("security-review-skill")
        optimizer = FrozenCandidateOptimizer(
            self.subject("security-review-skill-candidate")
        )

        cases = (
            ("baseline", "baseline validation", False),
            ("candidate", "candidate validation", True),
        )
        for variant, expected_reason, candidate_was_run in cases:
            with self.subTest(variant=variant):
                runtime = FakeRuntime(
                    {
                        ("decoded-path-validation", variant): RuntimeResult(
                            error="synthetic validation infrastructure failure"
                        )
                    }
                )
                orchestrator = EvalOrchestrator(
                    registry,
                    runtime,
                    output_root=self.output_root / variant,
                )

                result = await orchestrator.optimize(pack, subject, optimizer)

                self.assertFalse(result.accepted)
                self.assertIn(expected_reason, result.stop_reason)
                self.assertEqual(0, result.holdout_batch_count)
                self.assertIsNone(result.candidate_holdout)
                candidate_validation_calls = [
                    call
                    for call in runtime.calls
                    if call["subject_variant"] == "candidate"
                    and call["scenario_id"].endswith("-validation")
                ]
                self.assertEqual(candidate_was_run, bool(candidate_validation_calls))

    async def test_holdout_infrastructure_failure_is_not_a_quality_failure(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        runtime = FakeRuntime(
            {
                ("popen-command-holdout", "candidate"): RuntimeResult(
                    error="synthetic holdout infrastructure failure"
                )
            }
        )
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        )

        result = await orchestrator.optimize(
            pack,
            self.subject("security-review-skill"),
            FrozenCandidateOptimizer(
                self.subject("security-review-skill-candidate")
            ),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(1, result.holdout_batch_count)
        self.assertIsNotNone(result.candidate_holdout)
        self.assertIn("quality was not determined", result.stop_reason)

    async def test_runtime_exception_is_not_a_pass_and_workspace_is_cleaned(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        workspaces = []

        def fail_runtime(prepared, subject, context):
            del subject, context
            workspaces.append(prepared.workspace)
            raise RuntimeError("synthetic failure")

        runtime = FakeRuntime(
            {("currency-values-dev", "baseline"): fail_runtime}
        )
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        )

        run = await orchestrator.evaluate(
            pack, self.subject("csv-summary-skill"), ("dev",)
        )

        failed = run.scenario_by_id("currency-values-dev")
        self.assertEqual(GradeStatus.ERROR, failed.status)
        self.assertFalse(failed.passed)
        self.assertFalse(failed.hard_passed)
        self.assertIn("runtime_exception", failed.error)
        self.assertTrue(failed.grades)
        self.assertEqual(
            {GradeStatus.NOT_EVALUABLE},
            {grade.status for grade in failed.grades},
        )
        self.assertFalse(run.passed)
        self.assertEqual(1, len(workspaces))
        self.assertFalse(workspaces[0].exists())

    async def test_runtime_exception_preserves_partial_accounting_and_stops(self):
        class PartialRuntimeError(RuntimeError):
            def __init__(self):
                super().__init__("synthetic partial failure")
                self.usage = {"total_tokens": 100}
                self.trace = ({"kind": "tool_call", "name": "read_file"},)

        def fail_with_partial(prepared, subject, context):
            del prepared, subject, context
            raise PartialRuntimeError()

        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        runtime = FakeRuntime(
            {("currency-values-dev", "baseline"): fail_with_partial}
        )
        run = await EvalOrchestrator(
            registry,
            runtime,
            output_root=self.output_root,
            budget=RunBudget(max_total_tokens=1),
        ).evaluate(pack, self.subject("csv-summary-skill"), ("dev",))

        failed = run.scenario_by_id("currency-values-dev")
        self.assertEqual(1, len(runtime.calls))
        self.assertEqual(100, failed.observation.usage["total_tokens"])
        self.assertEqual(1, len(failed.observation.trace))
        self.assertEqual(GradeStatus.ERROR, failed.status)

    async def test_runtime_error_is_never_sent_to_optimizer(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        runtime = FakeRuntime(
            {
                ("command-injection-dev", "baseline"): RuntimeResult(
                    error="synthetic runtime failure"
                )
            }
        )
        optimizer = RecordingCandidateOptimizer(
            self.subject("security-review-skill-candidate")
        )
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        )

        result = await orchestrator.optimize(
            pack, self.subject("security-review-skill"), optimizer
        )

        self.assertFalse(result.accepted)
        self.assertEqual([], optimizer.calls)
        self.assertEqual((), result.trials)
        self.assertEqual(
            GradeStatus.ERROR,
            result.baseline_dev.scenario_by_id("command-injection-dev").status,
        )

    async def test_candidate_with_remaining_dev_failure_never_enters_validation(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        runtime = FakeRuntime(
            {
                ("command-injection-dev", "candidate"): {
                    "final_output": {"findings": []},
                    "trace": [
                        {
                            "kind": "tool_call",
                            "name": "read_file",
                            "payload": {"path": "command_injection.py"},
                        }
                    ],
                }
            }
        )
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        )

        result = await orchestrator.optimize(
            pack,
            self.subject("security-review-skill"),
            FrozenCandidateOptimizer(
                self.subject("security-review-skill-candidate")
            ),
        )

        self.assertFalse(result.accepted)
        self.assertTrue(result.trials)
        self.assertEqual(
            GradeStatus.FAIL,
            result.trials[0].dev_run.scenario_by_id("command-injection-dev").status,
        )
        self.assertFalse(result.trials[0].promoted_from_dev)
        self.assertIsNone(result.baseline_validation)
        self.assertIsNone(result.candidate_validation)
        self.assertFalse(
            any(
                call["subject_variant"] == "candidate"
                and call["scenario_id"].endswith("-validation")
                for call in runtime.calls
            )
        )

    async def test_runtime_context_does_not_expose_pack_or_work_roots(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        runtime = ContextCapturingRuntime()
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        )

        await orchestrator.evaluate(
            pack, self.subject("csv-summary-skill"), ("dev",)
        )

        self.assertTrue(runtime.contexts)
        for context in runtime.contexts:
            self.assertNotIn("pack_root", context.metadata)
            self.assertNotIn("work_root", context.metadata)

    async def test_runtime_receives_materialized_subject_uri_not_source_uri(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        source = self.subject("csv-summary-skill")
        original = (source / "SKILL.md").read_bytes()
        observed_subjects = []

        def mutate_visible_subject(prepared, subject, context):
            del prepared, context
            visible_root = Path(subject.uri).resolve()
            observed_subjects.append(
                (
                    visible_root,
                    Path(subject.metadata["path"]).resolve(),
                    Path(subject.metadata["materialized_path"]).resolve(),
                )
            )
            visible_skill = visible_root / "SKILL.md"
            visible_skill.chmod(0o644)
            visible_skill.write_text("# Runtime mutation\n", encoding="utf-8")
            return RuntimeResult(final_output={})

        orchestrator = EvalOrchestrator(
            registry,
            FakeRuntime({"default": mutate_visible_subject}),
            output_root=self.output_root,
        )

        with self.assertRaisesRegex(OrchestrationError, "modified"):
            await orchestrator.evaluate(pack, source, ("dev",))

        self.assertTrue(observed_subjects)
        for subject_uri, metadata_path, materialized_path in observed_subjects:
            self.assertNotEqual(source.resolve(), subject_uri)
            self.assertEqual(subject_uri, metadata_path)
            self.assertEqual(subject_uri, materialized_path)
        self.assertEqual(original, (source / "SKILL.md").read_bytes())

    async def test_delayed_final_scenario_cannot_pass_wall_time_budget(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        clock = [100.0]

        def monotonic():
            return clock[0]

        def delayed_success(prepared, subject, context):
            del prepared, subject, context
            clock[0] += 2.0
            output = {
                "currency": "USD",
                "total_revenue": 10.25,
                "orders": 3,
                "by_region": {"Unknown": 5.0, "West": 5.25},
            }
            return {
                "final_output": output,
                "artifacts": {"summary.json": output},
                "trace": [
                    {"kind": "tool_call", "name": "read_file"},
                    {"kind": "tool_call", "name": "write_file"},
                ],
            }

        runtime = FakeRuntime({"blank-region-refund-dev": delayed_success})
        orchestrator = EvalOrchestrator(
            registry,
            runtime,
            output_root=self.output_root,
            budget=RunBudget(max_wall_time_seconds=1.0),
        )

        with mock.patch("aceval.orchestrator.time.monotonic", side_effect=monotonic):
            run = await orchestrator.evaluate(
                pack, self.subject("csv-summary-skill-candidate"), ("dev",)
            )

        delayed = run.scenario_by_id("blank-region-refund-dev")
        self.assertEqual(GradeStatus.ERROR, delayed.status)
        self.assertFalse(delayed.hard_passed)
        self.assertIn("wall-time", delayed.error)

    async def test_pack_params_reach_subject_and_driver_but_not_runtime(self):
        pack_root = Path(self.temporary.name) / "parameterized-pack"
        shutil.copytree(ROOT / "evalpacks" / "csv-summary-smoke", pack_root)
        manifest_path = pack_root / "pack.yaml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["subject_contract"]["params"] = {"mode": "strict"}
        manifest["driver"]["params"] = {"max_rows": 50}
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        registry = ComponentRegistry()
        adapter = ParamCapturingSubjectAdapter()
        driver = ParamCapturingDriver()
        runtime = ContextCapturingRuntime()
        registry.register_subject_adapter(
            "skill_markdown_v1", adapter, kinds=("skill",)
        )
        registry.register_driver("artifact_workspace", driver)
        for component_id, grader in builtin_graders().items():
            registry.register_grader(component_id, grader)
        registry.register_optimizer("skill_markdown_v1", FrozenCandidateOptimizer)
        pack = EvalPackLoader(registry).load(pack_root)
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        )

        await orchestrator.evaluate(
            pack, self.subject("csv-summary-skill"), ("dev",)
        )

        self.assertEqual([{"mode": "strict"}] * 3, adapter.snapshot_params)
        self.assertEqual([{"mode": "strict"}], adapter.materialize_params)
        self.assertEqual(
            [{"max_rows": 50}, {"max_rows": 50}], driver.params
        )
        for context in runtime.contexts:
            self.assertNotIn("driver_params", context.metadata)

    async def test_driver_receives_gold_free_view_and_fixture_root(self):
        class InspectingDriver(ArtifactWorkspaceDriver):
            id = "isolated_driver"

            def __init__(self):
                super().__init__()
                self.views = []
                self.fixture_entries = []

            def required_capabilities(self, scenario):
                self.views.append(scenario)
                return super().required_capabilities(scenario)

            async def prepare(self, scenario, context):
                self.views.append(scenario)
                root = Path(context.metadata["fixture_root"])
                self.fixture_entries.append(
                    tuple(
                        item.relative_to(root).as_posix()
                        for item in sorted(root.rglob("*"))
                        if item.is_file()
                    )
                )
                return await super().prepare(scenario, context)

        class NonSimulatedRuntime:
            id = "non_simulated"
            capabilities = FakeRuntime().capabilities

            @property
            def profile(self):
                return RuntimeProfile(
                    adapter=self.id,
                    capabilities=self.capabilities,
                    metadata={"profile_complete": True},
                )

            async def execute(self, prepared, subject, context=None):
                del prepared, subject, context
                return RuntimeResult(error="synthetic stop")

        registry = component_registry()
        driver = InspectingDriver()
        registry.register_driver(driver.id, driver)
        pack_root = Path(self.temporary.name) / "isolated-pack"
        shutil.copytree(ROOT / "evalpacks" / "csv-summary-smoke", pack_root)
        manifest_path = pack_root / "pack.yaml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["driver"]["type"] = driver.id
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        pack = EvalPackLoader(registry).load(pack_root)

        await EvalOrchestrator(
            registry, NonSimulatedRuntime(), output_root=self.output_root
        ).evaluate(pack, self.subject("csv-summary-skill"), ("dev",))

        self.assertTrue(driver.views)
        for view in driver.views:
            self.assertFalse(hasattr(view, "oracle"))
            self.assertFalse(hasattr(view, "oracle_ref"))
            self.assertFalse(hasattr(view, "grader_params"))
            self.assertNotIn("fake_runtime", view.metadata)
        for entries in driver.fixture_entries:
            self.assertTrue(entries)
            self.assertFalse(
                any(
                    part in entry.split("/")
                    for entry in entries
                    for part in ("oracles", "scenarios", "schemas")
                )
            )

    async def test_pack_fixture_change_after_load_fails_before_execution(self):
        registry = component_registry()
        pack_root = Path(self.temporary.name) / "mutable-pack"
        shutil.copytree(ROOT / "evalpacks" / "csv-summary-smoke", pack_root)
        pack = EvalPackLoader(registry).load(pack_root)
        fixture = pack_root / "fixtures" / "dev" / "currency_values" / "input.csv"
        fixture.write_text("region,amount\nChanged,999\n", encoding="utf-8")
        runtime = FakeRuntime()
        orchestrator = EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        )

        with self.assertRaises(OrchestrationError):
            await orchestrator.evaluate(
                pack, self.subject("csv-summary-skill"), ("dev",)
            )

        self.assertEqual([], runtime.calls)

    async def test_forged_in_memory_pack_semantics_are_rejected(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        original = pack.scenarios[0]
        forged_scenario = replace(
            original,
            scenario=replace(
                original.scenario,
                grader_ids=(original.grader_ids[0],),
            ),
        )
        forged_pack = replace(
            pack,
            scenarios=(forged_scenario,) + pack.scenarios[1:],
        )
        runtime = FakeRuntime()

        with self.assertRaisesRegex(OrchestrationError, "semantic snapshot"):
            await EvalOrchestrator(
                registry, runtime, output_root=self.output_root
            ).evaluate(
                forged_pack,
                self.subject("csv-summary-skill-candidate"),
                ("dev",),
            )

        self.assertEqual([], runtime.calls)

    async def test_schema_resource_is_frozen_before_runtime_mutates_source(self):
        registry = component_registry()
        pack_root = Path(self.temporary.name) / "schema-freeze-pack"
        shutil.copytree(ROOT / "evalpacks" / "csv-summary-smoke", pack_root)
        pack = EvalPackLoader(registry).load(pack_root)
        schema_path = pack_root / "schemas" / "summary.schema.json"

        def mutate_schema(prepared, subject, context):
            del prepared, subject, context
            schema_path.write_text("{}\n", encoding="utf-8")
            invalid = {"currency": "USD"}
            return {
                "final_output": invalid,
                "artifacts": {"summary.json": invalid},
            }

        runtime = FakeRuntime({"default": mutate_schema})
        run = await EvalOrchestrator(
            registry, runtime, output_root=self.output_root
        ).evaluate(pack, self.subject("csv-summary-skill"), ("dev",))

        schema_grades = [
            grade
            for scenario in run.scenarios
            for grade in scenario.grades
            if grade.grader_id == "output-schema"
        ]
        self.assertTrue(schema_grades)
        self.assertTrue(
            all(grade.status == GradeStatus.FAIL for grade in schema_grades)
        )

    async def test_later_fixture_uses_run_snapshot_after_source_mutation(self):
        pack_root = Path(self.temporary.name) / "fixture-freeze-pack"
        shutil.copytree(ROOT / "evalpacks" / "csv-summary-smoke", pack_root)
        second_fixture = (
            pack_root / "fixtures" / "dev" / "blank_region_refund" / "input.csv"
        )
        original_second = second_fixture.read_bytes()

        class RecordingDriver(ArtifactWorkspaceDriver):
            def __init__(self):
                super().__init__()
                self.inputs = []

            async def prepare(self, scenario, context):
                prepared = await super().prepare(scenario, context)
                self.inputs.append((prepared.workspace / "input.csv").read_bytes())
                return prepared

        driver = RecordingDriver()
        registry = ComponentRegistry()
        registry.register_subject_adapter(
            "skill_markdown_v1", SkillMarkdownSubjectAdapter(), kinds=("skill",)
        )
        registry.register_driver("artifact_workspace", driver)
        for component_id, grader in builtin_graders().items():
            registry.register_grader(component_id, grader)
        registry.register_optimizer("skill_markdown_v1", FrozenCandidateOptimizer)
        pack = EvalPackLoader(registry).load(pack_root)
        runtime_contexts = []
        calls = [0]

        def mutate_later_fixture(prepared, subject, context):
            del prepared, subject
            runtime_contexts.append(context)
            calls[0] += 1
            if calls[0] == 1:
                second_fixture.write_text(
                    "region,amount\nMutated,999\n", encoding="utf-8"
                )
            return RuntimeResult(error="synthetic runtime stop")

        await EvalOrchestrator(
            registry,
            FakeRuntime({"default": mutate_later_fixture}),
            output_root=self.output_root,
        ).evaluate(pack, self.subject("csv-summary-skill"), ("dev",))

        self.assertEqual(2, len(driver.inputs))
        self.assertEqual(original_second, driver.inputs[1])
        for context in runtime_contexts:
            self.assertNotIn("pack_root", context.metadata)
            self.assertNotIn("frozen-pack", json.dumps(dict(context.metadata)))

    async def test_runtime_profile_and_budget_are_persisted_and_reused(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        runtime = ContextCapturingRuntime()
        budget = RunBudget(max_total_tokens=100, max_tool_calls=20)
        orchestrator = EvalOrchestrator(
            registry,
            runtime,
            output_root=self.output_root,
            budget=budget,
        )

        run = await orchestrator.evaluate(
            pack, self.subject("csv-summary-skill"), ("dev",)
        )

        self.assertEqual(budget, run.configured_budget)
        self.assertEqual(
            orchestrator.runtime_profile.profile_hash,
            run.runtime_profile.profile_hash,
        )
        self.assertTrue(runtime.contexts)
        self.assertTrue(
            all(
                context.runtime_profile.profile_hash
                == run.runtime_profile.profile_hash
                for context in runtime.contexts
            )
        )

    async def test_output_root_inside_pack_is_rejected_before_runtime(self):
        registry = component_registry()
        pack_root = Path(self.temporary.name) / "nested-output-pack"
        shutil.copytree(ROOT / "evalpacks" / "csv-summary-smoke", pack_root)
        pack = EvalPackLoader(registry).load(pack_root)
        runtime = FakeRuntime()

        with self.assertRaisesRegex(OrchestrationError, "output_root"):
            await EvalOrchestrator(
                registry,
                runtime,
                output_root=pack_root / "runs",
            ).evaluate(pack, self.subject("csv-summary-skill"), ("dev",))

        self.assertEqual([], runtime.calls)

    async def test_forged_candidate_base_path_and_hash_are_rejected(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        baseline = self.subject("security-review-skill")
        candidate = self.subject("security-review-skill-candidate")

        for mode in ("base", "path", "hash", "diff"):
            with self.subTest(mode=mode):
                output_root = self.output_root / mode
                orchestrator = EvalOrchestrator(
                    registry, FakeRuntime(), output_root=output_root
                )
                bridge = ForgedPatchBridge(mode, output_root, candidate)

                result = await orchestrator.optimize(pack, baseline, bridge)

                self.assertFalse(result.accepted)
                self.assertEqual((), result.trials)
                self.assertEqual(1, result.proposal_attempt_count)
                self.assertEqual(1, result.rejected_proposal_count)
                self.assertIn("optimizer protocol error", result.stop_reason)

    async def test_token_and_tool_budgets_are_shared_across_cases(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        cases = (
            (
                "token",
                RunBudget(max_total_tokens=5),
                RuntimeResult(
                    error="synthetic result",
                    usage={"total_tokens": 5},
                ),
            ),
            (
                "tool",
                RunBudget(max_tool_calls=1),
                RuntimeResult(
                    error="synthetic result",
                    trace=(TraceEvent(kind="tool_call", name="read_file"),),
                ),
            ),
        )
        for label, budget, scripted_result in cases:
            with self.subTest(budget=label):
                runtime = FakeRuntime({"default": scripted_result})
                orchestrator = EvalOrchestrator(
                    registry,
                    runtime,
                    output_root=self.output_root / label,
                    budget=budget,
                )

                run = await orchestrator.evaluate(
                    pack, self.subject("csv-summary-skill"), ("dev",)
                )

                self.assertEqual(1, len(runtime.calls))
                budget_failures = [
                    scenario
                    for scenario in run.scenarios
                    if scenario.error and "budget" in scenario.error
                ]
                self.assertEqual(1, len(budget_failures))

    async def test_optimizer_usage_exhaustion_returns_structured_result(self):
        class CostlyCandidateOptimizer(FrozenCandidateOptimizer):
            def propose(self, *args, **kwargs):
                snapshot = super().propose(*args, **kwargs)
                return replace(snapshot, usage={"total_tokens": 2})

        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        orchestrator = EvalOrchestrator(
            registry,
            FakeRuntime(),
            output_root=self.output_root,
            budget=RunBudget(max_total_tokens=1),
        )

        result = await orchestrator.optimize(
            pack,
            self.subject("security-review-skill"),
            CostlyCandidateOptimizer(
                self.subject("security-review-skill-candidate")
            ),
        )

        self.assertFalse(result.accepted)
        self.assertEqual((), result.trials)
        self.assertIn("optimization budget exhausted", result.stop_reason)
        self.assertEqual(1, result.proposal_attempt_count)
        self.assertEqual(2, result.experiment_usage["total_tokens"])
        self.assertEqual(
            "partial",
            result.experiment_usage["measurement_status"]["tokens"],
        )
        with self.assertRaises(TypeError):
            result.experiment_usage["total_tokens"] = 0

    async def test_rejected_optimizer_proposals_are_audited(self):
        class RejectingOptimizer:
            id = "skill_markdown_v1"
            proposal_contract = CANDIDATE_PATCH_OPTIMIZER_CONTRACT

            def propose(self, *args, **kwargs):
                del args, kwargs
                raise CandidateRejected(
                    "synthetic rejected proposal", {"total_tokens": 2}
                )

        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        result = await EvalOrchestrator(
            registry,
            FakeRuntime(),
            output_root=self.output_root,
            budget=RunBudget(max_total_tokens=10),
        ).optimize(
            pack,
            self.subject("security-review-skill"),
            RejectingOptimizer(),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(2, result.proposal_attempt_count)
        self.assertEqual(2, result.rejected_proposal_count)
        self.assertEqual(4, result.experiment_usage["total_tokens"])
        self.assertEqual(
            "partial",
            result.experiment_usage["measurement_status"]["tokens"],
        )
        self.assertEqual((), result.trials)

    async def test_invalid_candidate_patch_usage_is_audited_without_a_trial(self):
        class InvalidPatchOptimizer:
            id = "skill_markdown_v1"
            proposal_contract = CANDIDATE_PATCH_OPTIMIZER_CONTRACT

            def propose(self, base, diagnoses, constraints):
                del base, diagnoses, constraints
                return (
                    CandidatePatch(
                        base_hash="0" * 64,
                        unified_diff=(
                            "--- a/SKILL.md\n"
                            "+++ b/SKILL.md\n"
                            "@@ -1 +1 @@\n"
                            "-baseline\n"
                            "+candidate\n"
                        ),
                        allowed_paths=("SKILL.md",),
                        metadata={
                            "usage": {
                                "input_tokens": 2,
                                "output_tokens": 3,
                                "cost_usd": 0.01,
                            }
                        },
                    ),
                )

        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        result = await EvalOrchestrator(
            registry,
            FakeRuntime(),
            output_root=self.output_root,
        ).optimize(
            pack,
            self.subject("security-review-skill"),
            InvalidPatchOptimizer(),
        )

        self.assertFalse(result.accepted)
        self.assertEqual((), result.trials)
        self.assertEqual(1, result.proposal_attempt_count)
        self.assertEqual(1, result.rejected_proposal_count)
        self.assertEqual(5, result.experiment_usage["total_tokens"])
        self.assertAlmostEqual(0.01, result.experiment_usage["cost_usd"])

    async def test_fake_optimization_marks_unreported_tokens_and_cost_not_measured(self):
        registry = component_registry()
        pack = self.load_pack("security-review", registry)
        result = await EvalOrchestrator(
            registry,
            FakeRuntime(),
            output_root=self.output_root,
        ).optimize(
            pack,
            self.subject("security-review-skill"),
            FrozenCandidateOptimizer(
                self.subject("security-review-skill-candidate")
            ),
        )

        usage = result.experiment_usage
        self.assertIsNone(usage["total_tokens"])
        self.assertIsNone(usage["cost_usd"])
        self.assertEqual("not_measured", usage["measurement_status"]["tokens"])
        self.assertEqual("not_measured", usage["measurement_status"]["cost"])
        report_usage = to_report_dict(result)["summary"]["experiment_usage"]
        self.assertEqual("not_measured", report_usage["measurement_status"]["tokens"])
        self.assertEqual("not_measured", report_usage["measurement_status"]["cost"])

    def test_invalid_token_usage_does_not_mark_tokens_as_measured(self):
        for value in (0.9, float("nan")):
            with self.subTest(value=value):
                ledger = _BudgetLedger(RunBudget())

                with self.assertRaisesRegex(
                    OrchestrationError, "invalid non-negative integer usage"
                ):
                    ledger.consume_usage({"total_tokens": value})

                snapshot = ledger.snapshot()
                self.assertIsNone(snapshot["total_tokens"])
                self.assertEqual(
                    "not_measured", snapshot["measurement_status"]["tokens"]
                )
                self.assertEqual(0, snapshot["measured_calls"]["tokens"])

    async def test_non_finite_runtime_usage_is_an_infrastructure_error(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        usages = (
            {"cost_usd": float("nan")},
            {"total_tokens": 1, "input_tokens": float("nan")},
        )
        for index, usage in enumerate(usages):
            with self.subTest(usage=usage):
                runtime = FakeRuntime(
                    {"default": RuntimeResult(usage=usage)}
                )
                orchestrator = EvalOrchestrator(
                    registry,
                    runtime,
                    output_root=self.output_root / str(index),
                )

                run = await orchestrator.evaluate(
                    pack, self.subject("csv-summary-skill"), ("dev",)
                )

                self.assertFalse(run.passed)
                self.assertIn(
                    "invalid non-negative", run.scenarios[0].error
                )
                json.dumps(to_report_dict(run), allow_nan=False)

    async def test_fractional_token_usage_is_an_infrastructure_error(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        runtime = FakeRuntime(
            {"default": RuntimeResult(usage={"total_tokens": 0.9})}
        )
        run = await EvalOrchestrator(
            registry,
            runtime,
            output_root=self.output_root,
            budget=RunBudget(max_total_tokens=1),
        ).evaluate(pack, self.subject("csv-summary-skill"), ("dev",))

        self.assertEqual(GradeStatus.ERROR, run.scenarios[0].status)
        self.assertIn("non-negative integer usage", run.scenarios[0].error)

    async def test_usage_aliases_and_mapping_trace_are_charged_conservatively(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        runtime = FakeRuntime(
            {
                "default": RuntimeResult(
                    error="synthetic result",
                    usage={
                        "total_tokens": 1,
                        "input_tokens": 3,
                        "output_tokens": 4,
                        "cost_usd": 0.0,
                        "total_cost_usd": 2.0,
                    },
                    trace=(
                        {"kind": None, "type": "tool_call", "name": "read_file"},
                        {"kind": "", "type": "tool", "name": "read_file"},
                    ),
                )
            }
        )
        run = await EvalOrchestrator(
            registry,
            runtime,
            output_root=self.output_root,
            budget=RunBudget(
                max_total_tokens=6, max_cost_usd=1.0, max_tool_calls=1
            ),
        ).evaluate(pack, self.subject("csv-summary-skill"), ("dev",))

        self.assertEqual(1, len(runtime.calls))
        self.assertEqual(GradeStatus.ERROR, run.scenarios[0].status)
        self.assertIn("budget exceeded", run.scenarios[0].error)

    async def test_collect_failure_preserves_and_charges_runtime_usage_and_trace(self):
        registry = component_registry()
        pack = self.load_pack("csv-summary-smoke", registry)
        driver = registry.driver(pack.manifest.driver.type)

        async def failing_collect(prepared, result):
            del prepared, result
            raise RuntimeError("synthetic collect failure")

        driver.collect = failing_collect
        runtime = FakeRuntime(
            {
                "default": RuntimeResult(
                    usage={"total_tokens": 5},
                    trace=({"kind": "tool_call", "name": "read_file"},),
                )
            }
        )
        run = await EvalOrchestrator(
            registry,
            runtime,
            output_root=self.output_root,
            budget=RunBudget(max_total_tokens=4),
        ).evaluate(pack, self.subject("csv-summary-skill"), ("dev",))

        first = run.scenarios[0]
        self.assertEqual(1, len(runtime.calls))
        self.assertEqual(5, first.observation.usage["total_tokens"])
        self.assertEqual(1, len(first.observation.trace))
        self.assertIn("budget exceeded", first.error)

    async def test_opaque_holdout_ref_cannot_execute_without_resolver(self):
        registry = component_registry()
        pack_root = Path(self.temporary.name) / "opaque-holdout-pack"
        shutil.copytree(ROOT / "evalpacks" / "security-review", pack_root)
        manifest_path = pack_root / "pack.yaml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["suite"]["holdout_ref"] = "hidden-holdout-v1"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        pack = EvalPackLoader(registry).load(pack_root)
        orchestrator = EvalOrchestrator(
            registry, FakeRuntime(), output_root=self.output_root
        )

        with self.assertRaises(OrchestrationError):
            await orchestrator.optimize(
                pack,
                self.subject("security-review-skill"),
                FrozenCandidateOptimizer(
                    self.subject("security-review-skill-candidate")
                ),
            )


if __name__ == "__main__":
    unittest.main()
