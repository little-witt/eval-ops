"""Deterministic EvalOps lifecycle and regression gates.

The orchestrator owns experiment control. Domain behavior stays in EvalPacks,
drivers, runtimes, graders, and the constrained Skill optimizer.
"""

from __future__ import annotations

import difflib
import hashlib
import inspect
import json
import math
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    CANDIDATE_PATCH_OPTIMIZER_CONTRACT,
    CandidatePatch,
    DriverScenarioView,
    FrozenEvalPack,
    FrozenOracle,
    FrozenScenario,
    GradeResult,
    GradeStatus,
    ImprovementMode,
    ObjectiveSpec,
    PatchConstraints,
    RunBudget,
    RunContext,
    RunObservation,
    RuntimeProfile,
    RuntimeResult,
    SubjectSnapshot,
    is_tool_call_event,
)
from .optimizer import (
    CandidateRejected,
    FailureEvidence,
    SkillOptimizerBridge,
    SkillPatchPolicy,
    SKILL_MARKDOWN_GENERATOR_CONTRACT,
    SKILL_MARKDOWN_IMPROVER_CONTRACT,
    TuneEvidence,
)
from .objectives import (
    ObjectiveComparison,
    compare_objective,
    measure_objective,
)
from .pack import EvalPackLoader, PackError
from .pack_lifecycle import (
    CALIBRATION_FROZEN,
    CALIBRATION_LEGACY,
    pack_calibration_status,
)
from .subjects import with_variant


class OrchestrationError(RuntimeError):
    """Raised when an evaluation cannot start without violating its contract."""


class _BudgetExceeded(OrchestrationError):
    pass


class _InvalidUsage(_BudgetExceeded):
    """Usage telemetry is malformed and cannot be used for accounting."""


@dataclass
class _BudgetLedger:
    budget: RunBudget
    tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    usage_expected_count: int = 0
    usage_measured_count: int = 0
    token_measured_count: int = 0
    cost_measured_count: int = 0
    trace_measured_count: int = 0
    started_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = time.monotonic()

    def remaining(self, scenario_timeout: int) -> RunBudget:
        wall_time = float(scenario_timeout)
        if self.budget.max_wall_time_seconds is not None:
            remaining_wall = float(self.budget.max_wall_time_seconds) - (
                time.monotonic() - self.started_at
            )
            if remaining_wall <= 0:
                raise _BudgetExceeded("global wall-time budget is exhausted")
            wall_time = min(wall_time, remaining_wall)
        return RunBudget(
            max_wall_time_seconds=wall_time,
            max_total_tokens=self._remaining_int(self.budget.max_total_tokens, self.tokens),
            max_cost_usd=self._remaining_float(self.budget.max_cost_usd, self.cost_usd),
            max_tool_calls=self._remaining_int(
                self.budget.max_tool_calls, self.tool_calls
            ),
        )

    def ensure_available(self) -> None:
        if (
            self.budget.max_wall_time_seconds is not None
            and time.monotonic() - self.started_at
            >= self.budget.max_wall_time_seconds
        ):
            raise _BudgetExceeded("global wall-time budget is exhausted")
        checks = (
            ("token", self.budget.max_total_tokens, self.tokens),
            ("cost", self.budget.max_cost_usd, self.cost_usd),
            ("tool-call", self.budget.max_tool_calls, self.tool_calls),
        )
        for label, limit, consumed in checks:
            if limit is not None and consumed >= limit:
                raise _BudgetExceeded("global %s budget is exhausted" % label)

    def snapshot(self) -> Mapping[str, Any]:
        def status(measured: int, expected: int) -> str:
            if measured == 0:
                return "not_measured"
            return "measured" if measured == expected else "partial"

        return {
            "total_tokens": self.tokens if self.token_measured_count else None,
            "cost_usd": self.cost_usd if self.cost_measured_count else None,
            "tool_calls": self.tool_calls if self.trace_measured_count else None,
            "elapsed_seconds": max(0.0, time.monotonic() - self.started_at),
            "measurement_status": {
                "usage": status(
                    self.usage_measured_count, self.usage_expected_count
                ),
                "tokens": status(
                    self.token_measured_count, self.usage_expected_count
                ),
                "cost": status(
                    self.cost_measured_count, self.usage_expected_count
                ),
                "tool_calls": (
                    "measured" if self.trace_measured_count else "not_measured"
                ),
            },
            "measured_calls": {
                "usage": self.usage_measured_count,
                "tokens": self.token_measured_count,
                "cost": self.cost_measured_count,
                "trace": self.trace_measured_count,
                "expected_usage": self.usage_expected_count,
            },
        }

    def consume_usage(
        self, usage: Mapping[str, Any], trace: Optional[Sequence[Any]] = None
    ) -> None:
        self.usage_expected_count += 1
        if not isinstance(usage, Mapping):
            raise _InvalidUsage("runtime usage must be an object")
        token_keys = (
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
        )
        has_tokens = any(key in usage for key in token_keys)
        has_cost = any(key in usage for key in ("cost_usd", "total_cost_usd"))
        for key in (
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
        ):
            if key in usage:
                self._usage_token(usage[key], key)
        for key in ("cost_usd", "total_cost_usd"):
            if key in usage:
                self._usage_number(usage[key], key)
        tokens = self._token_count(usage)
        costs = [
            self._usage_number(usage[key], key)
            for key in ("cost_usd", "total_cost_usd")
            if key in usage
        ]
        cost_usd = max(costs, default=0.0)
        tool_calls = sum(is_tool_call_event(item) for item in (trace or ()))

        # Only valid telemetry is marked as measured.  It is committed before
        # limit checks so a valid call that exceeds a budget remains auditable.
        if usage:
            self.usage_measured_count += 1
        if has_tokens:
            self.token_measured_count += 1
        if has_cost:
            self.cost_measured_count += 1
        if trace is not None:
            self.trace_measured_count += 1
        self.tokens += tokens
        self.cost_usd += cost_usd
        self.tool_calls += tool_calls
        checks = (
            ("token", self.budget.max_total_tokens, self.tokens),
            ("cost", self.budget.max_cost_usd, self.cost_usd),
            ("tool-call", self.budget.max_tool_calls, self.tool_calls),
        )
        for label, limit, consumed in checks:
            if limit is not None and consumed > limit:
                raise _BudgetExceeded(
                    "global %s budget exceeded: %s > %s" % (label, consumed, limit)
                )
        if (
            self.budget.max_wall_time_seconds is not None
            and time.monotonic() - self.started_at
            >= self.budget.max_wall_time_seconds
        ):
            raise _BudgetExceeded("global wall-time budget is exhausted")

    @staticmethod
    def _token_count(usage: Mapping[str, Any]) -> int:
        totals = []
        if "total_tokens" in usage:
            totals.append(
                _BudgetLedger._usage_token(usage["total_tokens"], "total_tokens")
            )
        for keys in (("input_tokens", "output_tokens"), ("prompt_tokens", "completion_tokens")):
            if any(key in usage for key in keys):
                totals.append(
                    sum(
                        _BudgetLedger._usage_token(usage[key], key)
                        for key in keys
                        if key in usage
                    )
                )
        return max(totals, default=0)

    @staticmethod
    def _usage_token(value: Any, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise _InvalidUsage(
                "invalid non-negative integer usage value: %s" % label
            )
        return value

    @staticmethod
    def _usage_number(value: Any, label: str) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise _InvalidUsage(
                "invalid non-negative finite usage value: %s" % label
            )
        return float(value)

    @staticmethod
    def _remaining_int(limit: Optional[int], consumed: int) -> Optional[int]:
        return None if limit is None else max(0, int(limit) - consumed)

    @staticmethod
    def _remaining_float(limit: Optional[float], consumed: float) -> Optional[float]:
        return None if limit is None else max(0.0, float(limit) - consumed)


@dataclass(frozen=True)
class ScenarioEvaluation:
    scenario_id: str
    split: str
    status: GradeStatus
    grades: Tuple[GradeResult, ...] = ()
    observation: Optional[RunObservation] = None
    duration_seconds: float = 0.0
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status == GradeStatus.PASS

    @property
    def hard_passed(self) -> bool:
        if self.error or not self.grades:
            return False
        if any(
            grade.status in (GradeStatus.ERROR, GradeStatus.NOT_EVALUABLE)
            for grade in self.grades
        ):
            return False
        hard_grades = tuple(grade for grade in self.grades if grade.hard)
        return bool(hard_grades) and all(
            grade.status == GradeStatus.PASS for grade in hard_grades
        )


@dataclass(frozen=True)
class EvalRun:
    run_id: str
    pack_name: str
    pack_hash: str
    suite_hash: str
    subject_hash: str
    subject_uri: str
    subject_variant: str
    runtime_id: str
    requested_splits: Tuple[str, ...]
    scenarios: Tuple[ScenarioEvaluation, ...]
    duration_seconds: float
    run_dir: Path
    simulated: bool = False
    limitations: Tuple[str, ...] = ()
    runtime_profile: Optional[RuntimeProfile] = None
    configured_budget: Optional[RunBudget] = None

    @property
    def pass_count(self) -> int:
        return sum(item.passed for item in self.scenarios)

    @property
    def hard_pass_count(self) -> int:
        return sum(item.hard_passed for item in self.scenarios)

    @property
    def total_count(self) -> int:
        return len(self.scenarios)

    @property
    def pass_rate(self) -> float:
        return self.pass_count / self.total_count if self.total_count else 0.0

    @property
    def hard_pass_rate(self) -> float:
        return self.hard_pass_count / self.total_count if self.total_count else 0.0

    @property
    def passed(self) -> bool:
        return bool(self.scenarios) and self.hard_pass_count == self.total_count

    def scenario_by_id(self, scenario_id: str) -> ScenarioEvaluation:
        for item in self.scenarios:
            if item.scenario_id == scenario_id:
                return item
        raise KeyError(scenario_id)


@dataclass(frozen=True)
class ComparisonResult:
    baseline: EvalRun
    candidate: EvalRun
    paired_uplift: Optional[float]
    improvements: Tuple[str, ...]
    hard_regressions: Tuple[str, ...]

    @property
    def evaluable(self) -> bool:
        return all(
            item.error is None
            and item.status in (GradeStatus.PASS, GradeStatus.FAIL)
            for run in (self.baseline, self.candidate)
            for item in run.scenarios
        ) and bool(self.baseline.scenarios and self.candidate.scenarios)

    @property
    def status(self) -> str:
        scenarios = self.baseline.scenarios + self.candidate.scenarios
        if any(item.error or item.status == GradeStatus.ERROR for item in scenarios):
            return "error"
        if any(item.status == GradeStatus.NOT_EVALUABLE for item in scenarios):
            return "not_evaluable"
        return "pass" if self.accepted else "fail"

    @property
    def accepted(self) -> bool:
        return (
            self.evaluable
            and self.candidate.passed
            and not self.hard_regressions
            and self.paired_uplift is not None
            and self.paired_uplift >= 0
        )


@dataclass(frozen=True)
class CandidateTrial:
    round_index: int
    candidate_id: str
    candidate_path: Path
    candidate_hash: str
    parent_hash: str
    patch: str
    rationale: str
    dev_run: EvalRun
    promoted_from_dev: bool
    rejection_reason: Optional[str] = None
    usage: Mapping[str, Any] = None
    objective_comparison: Optional[ObjectiveComparison] = None


@dataclass(frozen=True)
class ValidationAttempt:
    candidate_id: str
    candidate_hash: str
    run: EvalRun
    passed_gate: bool
    hard_regressions: Tuple[str, ...] = ()
    objective_comparison: Optional[ObjectiveComparison] = None


@dataclass(frozen=True)
class OptimizationResult:
    optimization_id: str
    pack_name: str
    baseline_dev: EvalRun
    trials: Tuple[CandidateTrial, ...] = ()
    validation_attempts: Tuple[ValidationAttempt, ...] = ()
    baseline_validation: Optional[EvalRun] = None
    candidate_validation: Optional[EvalRun] = None
    baseline_holdout: Optional[EvalRun] = None
    candidate_holdout: Optional[EvalRun] = None
    holdout_batch_count: int = 0
    holdout_pair_count: int = 0
    selected_candidate_id: Optional[str] = None
    selected_candidate_path: Optional[Path] = None
    selected_candidate_hash: Optional[str] = None
    accepted: bool = False
    stop_reason: str = ""
    output_dir: Optional[Path] = None
    limitations: Tuple[str, ...] = ()
    experiment_usage: Mapping[str, Any] = None
    proposal_attempt_count: int = 0
    rejected_proposal_count: int = 0
    duplicate_proposal_count: int = 0
    mode: str = ImprovementMode.REPAIR.value
    goal: str = ""
    objective: Optional[ObjectiveSpec] = None
    dev_objective: Optional[ObjectiveComparison] = None
    validation_objective: Optional[ObjectiveComparison] = None
    holdout_objective: Optional[ObjectiveComparison] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trials", tuple(self.trials))
        object.__setattr__(
            self, "validation_attempts", tuple(self.validation_attempts)
        )
        object.__setattr__(
            self,
            "experiment_usage",
            MappingProxyType(dict(self.experiment_usage or {})),
        )
        if self.holdout_batch_count not in (0, 1):
            raise ValueError("holdout_batch_count must be 0 or 1")
        expected = 1 if self.candidate_holdout is not None else 0
        if self.holdout_batch_count != expected:
            raise ValueError(
                "holdout_batch_count must match whether candidate_holdout was run"
            )
        if self.holdout_pair_count not in (0, 1):
            raise ValueError("holdout_pair_count must be 0 or 1")
        expected_pair = int(
            self.baseline_holdout is not None and self.candidate_holdout is not None
        )
        if self.holdout_pair_count != expected_pair:
            raise ValueError(
                "holdout_pair_count must match paired baseline/candidate holdout runs"
            )


def _variant(snapshot: SubjectSnapshot) -> str:
    value = snapshot.metadata.get("variant") if isinstance(snapshot.metadata, Mapping) else None
    return str(value or Path(snapshot.uri).name or "subject")


def _overall_status(grades: Sequence[GradeResult], error: Optional[str]) -> GradeStatus:
    if error:
        return GradeStatus.ERROR
    if not grades:
        return GradeStatus.NOT_EVALUABLE
    statuses = {grade.status for grade in grades}
    if GradeStatus.ERROR in statuses:
        return GradeStatus.ERROR
    if GradeStatus.NOT_EVALUABLE in statuses:
        return GradeStatus.NOT_EVALUABLE
    if GradeStatus.FAIL in statuses:
        return GradeStatus.FAIL
    return GradeStatus.PASS


def _comparison(baseline: EvalRun, candidate: EvalRun) -> ComparisonResult:
    baseline_ids = {item.scenario_id for item in baseline.scenarios}
    candidate_ids = {item.scenario_id for item in candidate.scenarios}
    if baseline_ids != candidate_ids:
        raise OrchestrationError("paired comparison requires identical scenario IDs")
    quality_evaluable = all(
        item.error is None
        and item.status in (GradeStatus.PASS, GradeStatus.FAIL)
        for run in (baseline, candidate)
        for item in run.scenarios
    ) and bool(baseline.scenarios and candidate.scenarios)
    improvements = []
    regressions = []
    for scenario_id in sorted(baseline_ids):
        old = baseline.scenario_by_id(scenario_id)
        new = candidate.scenario_by_id(scenario_id)
        if quality_evaluable and not old.hard_passed and new.hard_passed:
            improvements.append(scenario_id)
        if quality_evaluable and old.hard_passed and not new.hard_passed:
            regressions.append(scenario_id)
    return ComparisonResult(
        baseline=baseline,
        candidate=candidate,
        paired_uplift=(
            candidate.hard_pass_rate - baseline.hard_pass_rate
            if quality_evaluable
            else None
        ),
        improvements=tuple(improvements),
        hard_regressions=tuple(regressions),
    )


class EvalOrchestrator:
    """Run any validated EvalPack through the common lifecycle."""

    def __init__(
        self,
        registry: Any,
        runtime: Any,
        output_root: Path = Path(".aceval/runs"),
        budget: Optional[RunBudget] = None,
    ) -> None:
        self.registry = registry
        self.runtime = runtime
        self.output_root = Path(output_root).expanduser().resolve()
        self.budget = budget or RunBudget()
        for name, value in (
            ("max_wall_time_seconds", self.budget.max_wall_time_seconds),
            ("max_total_tokens", self.budget.max_total_tokens),
            ("max_cost_usd", self.budget.max_cost_usd),
            ("max_tool_calls", self.budget.max_tool_calls),
        ):
            if value is not None and value <= 0:
                raise ValueError("%s must be positive" % name)
        self.runtime_profile = self._capture_runtime_profile()

    async def evaluate(
        self,
        pack: FrozenEvalPack,
        subject: Any,
        splits: Iterable[str] = ("dev",),
        run_label: str = "run",
    ) -> EvalRun:
        """Evaluate public dev/validation splits with a fresh experiment budget."""

        return await self._evaluate(pack, subject, splits, run_label)

    async def _evaluate(
        self,
        pack: FrozenEvalPack,
        subject: Any,
        splits: Iterable[str],
        run_label: str,
        *,
        allow_holdout: bool = False,
        ledger: Optional[_BudgetLedger] = None,
    ) -> EvalRun:
        requested_splits = tuple(dict.fromkeys(str(item) for item in splits))
        unknown = set(requested_splits).difference({"dev", "validation", "holdout"})
        if unknown:
            raise OrchestrationError("unknown split(s): %s" % ", ".join(sorted(unknown)))
        if "holdout" in requested_splits and not allow_holdout:
            raise OrchestrationError(
                "holdout is reserved for the controlled final optimization gate"
            )
        self._verify_pack_integrity(pack)
        self._ensure_output_outside_pack(pack)
        self._ensure_splits_resolved(pack, requested_splits)
        selected = tuple(item for item in pack.scenarios if item.split in requested_splits)
        if not selected:
            raise OrchestrationError("no scenarios selected for splits: %s" % ", ".join(requested_splits))

        adapter = self.registry.subject_adapter(pack.manifest.subject_contract.adapter)
        subject_params = pack.manifest.subject_contract.params
        snapshot = (
            subject
            if isinstance(subject, SubjectSnapshot)
            else adapter.snapshot(str(subject), subject_params)
        )
        if snapshot.kind not in pack.manifest.subject_contract.kinds:
            raise OrchestrationError(
                "subject kind %r is not accepted by Pack %s"
                % (snapshot.kind, pack.manifest.metadata.name)
            )
        driver = self.registry.driver(pack.manifest.driver.type)
        self._check_runtime_capabilities(pack, driver, selected)

        run_id = "%s-%s" % (run_label, uuid.uuid4().hex[:12])
        run_dir = self.output_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        frozen_pack_root = self._materialize_pack_snapshot(
            pack, run_dir / "frozen-pack"
        )
        materialized = adapter.materialize(
            snapshot, run_dir / "subject", subject_params
        )
        materialized_entrypoint = materialized / pack.manifest.subject_contract.entrypoint
        if not materialized_entrypoint.is_file():
            raise OrchestrationError("materialized Subject is missing its declared entrypoint")
        try:
            materialized_snapshot = adapter.snapshot(str(materialized), subject_params)
        except Exception as exc:
            raise OrchestrationError("cannot verify materialized Subject: %s" % exc) from exc
        if (
            materialized_snapshot.kind != snapshot.kind
            or materialized_snapshot.content_hash != snapshot.content_hash
        ):
            raise OrchestrationError("materialized Subject differs from its frozen snapshot")
        runtime_metadata = dict(snapshot.metadata)
        runtime_metadata.update({"materialized_path": str(materialized), "path": str(materialized)})
        runtime_subject = replace(
            snapshot,
            uri=str(materialized),
            metadata=runtime_metadata,
        )
        profile = self.runtime_profile
        runtime_id = profile.adapter

        started = time.monotonic()
        results = []
        ledger = ledger or _BudgetLedger(self.budget)
        budget_error = None
        for scenario in selected:
            if budget_error is not None:
                results.append(self._budget_failure(pack, scenario, budget_error))
                continue
            try:
                ledger.ensure_available()
            except _BudgetExceeded as exc:
                budget_error = str(exc)
                results.append(self._budget_failure(pack, scenario, budget_error))
                continue
            try:
                result = await self._evaluate_scenario(
                    pack,
                    scenario,
                    runtime_subject,
                    driver,
                    profile,
                    run_dir,
                    frozen_pack_root,
                    ledger,
                )
            except _BudgetExceeded as exc:
                budget_error = str(exc)
                result = self._budget_failure(pack, scenario, budget_error)
            try:
                if result.observation is not None:
                    ledger.consume_usage(
                        result.observation.usage, result.observation.trace
                    )
            except _BudgetExceeded as exc:
                budget_error = str(exc)
                result = replace(
                    result,
                    status=GradeStatus.ERROR,
                    grades=tuple(self._not_evaluable_grades(pack, scenario, budget_error)),
                    error=budget_error,
                )
            if (
                budget_error is None
                and result.observation is not None
                and result.observation.metadata.get("runtime_adapter_exception")
            ):
                budget_error = (
                    "runtime adapter raised an exception; remaining scenarios were not run"
                )
            results.append(result)
        try:
            final_materialized_snapshot = adapter.snapshot(str(materialized), subject_params)
        except Exception as exc:
            raise OrchestrationError(
                "materialized Subject became invalid during execution: %s" % exc
            ) from exc
        if (
            final_materialized_snapshot.kind != snapshot.kind
            or final_materialized_snapshot.content_hash != snapshot.content_hash
        ):
            raise OrchestrationError("Runtime modified the frozen Subject snapshot")
        duration = time.monotonic() - started
        limitations = [
            "Scores are calibrated for this runtime profile; cross-runtime equivalence is not assumed.",
            "The MVP uses same-user workspace isolation, not a hostile-code sandbox.",
            "A single attempt cannot measure flake rate.",
        ]
        simulated = runtime_id == "fake" or bool(
            profile.metadata.get("simulated", False)
        )
        if simulated:
            limitations.append(
                "SIMULATION: FakeRuntime uses preregistered outputs and does not measure model execution."
            )
        if not profile.metadata.get("profile_complete", False):
            limitations.append(
                "Runtime profile is incomplete; model and inference settings may not be reproducible."
            )
        return EvalRun(
            run_id=run_id,
            pack_name=pack.manifest.metadata.name,
            pack_hash=pack.pack_hash,
            suite_hash=pack.suite_hash,
            subject_hash=snapshot.content_hash,
            subject_uri=snapshot.uri,
            subject_variant=_variant(snapshot),
            runtime_id=runtime_id,
            requested_splits=requested_splits,
            scenarios=tuple(results),
            duration_seconds=duration,
            run_dir=run_dir,
            simulated=simulated,
            limitations=tuple(limitations),
            runtime_profile=profile,
            configured_budget=self.budget,
        )

    async def compare(
        self,
        pack: FrozenEvalPack,
        baseline: Any,
        candidate: Any,
        splits: Iterable[str] = ("dev",),
    ) -> ComparisonResult:
        frozen_splits = tuple(splits)
        ledger = _BudgetLedger(self.budget)
        adapter = self.registry.subject_adapter(pack.manifest.subject_contract.adapter)
        subject_params = pack.manifest.subject_contract.params
        baseline_snapshot = (
            baseline
            if isinstance(baseline, SubjectSnapshot)
            else adapter.snapshot(str(baseline), subject_params)
        )
        candidate_snapshot = (
            candidate
            if isinstance(candidate, SubjectSnapshot)
            else adapter.snapshot(str(candidate), subject_params)
        )
        baseline_run = await self._evaluate(
            pack,
            with_variant(baseline_snapshot, "baseline"),
            frozen_splits,
            "baseline",
            ledger=ledger,
        )
        candidate_run = await self._evaluate(
            pack,
            with_variant(candidate_snapshot, "candidate"),
            frozen_splits,
            "candidate",
            ledger=ledger,
        )
        return _comparison(baseline_run, candidate_run)

    async def optimize(
        self,
        pack: FrozenEvalPack,
        subject: Any,
        candidate_optimizer: Any,
        max_rounds: Optional[int] = None,
        mode: Optional[str] = None,
        goal: Optional[str] = None,
        objective: Optional[ObjectiveSpec] = None,
    ) -> OptimizationResult:
        policy = pack.manifest.optimizer_policy
        if policy is None:
            raise OrchestrationError("EvalPack is eval-only and declares no optimizer policy")
        calibration_status = pack_calibration_status(pack)
        if calibration_status not in (CALIBRATION_FROZEN, CALIBRATION_LEGACY):
            raise OrchestrationError(
                "EvalPack calibration_status is %s; run calibration and explicitly "
                "freeze the Pack before optimizing a Skill" % calibration_status
            )
        if tuple(policy.visible_splits) != ("dev",):
            raise OrchestrationError("optimizer visibility must be exactly the dev split")
        try:
            requested_mode = ImprovementMode(mode or policy.mode)
        except ValueError as exc:
            raise OrchestrationError("mode must be auto, repair, or tune") from exc
        active_goal = str(policy.goal if goal is None else goal).strip()
        active_objective = objective or policy.objective
        optimizer_id = getattr(candidate_optimizer, "id", None)
        if optimizer_id != policy.adapter:
            raise OrchestrationError(
                "optimizer %r does not satisfy declared adapter %r"
                % (optimizer_id, policy.adapter)
            )
        declared_splits = tuple(
            split
            for split in ("dev", "validation", "holdout")
            if self._split_declared(pack, split)
        )
        self._verify_pack_integrity(pack)
        self._ensure_output_outside_pack(pack)
        self._ensure_splits_resolved(pack, declared_splits)

        optimization_id = "optimize-%s" % uuid.uuid4().hex[:12]
        optimization_dir = self.output_root / optimization_id
        candidate_root = optimization_dir / "candidates"
        candidate_root.mkdir(parents=True, exist_ok=False)
        adapter = self.registry.subject_adapter(pack.manifest.subject_contract.adapter)
        subject_params = pack.manifest.subject_contract.params
        base_snapshot = (
            subject
            if isinstance(subject, SubjectSnapshot)
            else adapter.snapshot(str(subject), subject_params)
        )
        ledger = _BudgetLedger(self.budget)
        proposal_attempt_count = 0
        rejected_proposal_count = 0
        duplicate_proposal_count = 0
        active_mode = requested_mode

        def finish_optimization(**values: Any) -> OptimizationResult:
            values.setdefault("optimization_id", optimization_id)
            values.setdefault("pack_name", pack.manifest.metadata.name)
            values.setdefault("experiment_usage", ledger.snapshot())
            values.setdefault("proposal_attempt_count", proposal_attempt_count)
            values.setdefault("rejected_proposal_count", rejected_proposal_count)
            values.setdefault("duplicate_proposal_count", duplicate_proposal_count)
            values.setdefault("mode", active_mode.value)
            values.setdefault("goal", active_goal)
            values.setdefault("objective", active_objective)
            return OptimizationResult(**values)

        baseline_dev = await self._evaluate(
            pack, base_snapshot, ("dev",), "baseline-dev", ledger=ledger
        )
        if self._has_non_skill_failure(baseline_dev):
            return finish_optimization(
                baseline_dev=baseline_dev,
                accepted=False,
                stop_reason="dev contains runtime, evaluator, or missing-evidence failures; Skill was not modified",
                output_dir=optimization_dir,
                limitations=(
                    "Only deterministic hard FAIL results may become optimizer evidence.",
                ),
            )
        failures = self._failure_evidence(baseline_dev)
        if requested_mode == ImprovementMode.AUTO:
            active_mode = (
                ImprovementMode.REPAIR if failures else ImprovementMode.TUNE
            )
        if active_mode == ImprovementMode.REPAIR:
            # Auto mode may carry a tune objective for the eventual passing
            # baseline.  A repair run must not report or optimize that metric.
            active_objective = None
        if active_mode == ImprovementMode.REPAIR and not failures:
            return finish_optimization(
                baseline_dev=baseline_dev,
                accepted=False,
                stop_reason="baseline already passes dev; no evidence-authorized change",
                output_dir=optimization_dir,
                limitations=("No candidate is generated without dev failure evidence.",),
            )
        if active_mode == ImprovementMode.TUNE:
            if failures or not baseline_dev.passed:
                return finish_optimization(
                    baseline_dev=baseline_dev,
                    accepted=False,
                    stop_reason=(
                        "baseline is not tune-eligible because dev hard gates do not all pass; run repair first"
                    ),
                    output_dir=optimization_dir,
                    limitations=(
                        "Tune mode never trades away correctness or safety hard gates.",
                    ),
                )
            if active_objective is None:
                return finish_optimization(
                    baseline_dev=baseline_dev,
                    accepted=False,
                    stop_reason="tune mode requires a measurable objective",
                    output_dir=optimization_dir,
                    limitations=(
                        "A natural-language goal alone cannot authorize an optimization result.",
                    ),
                )
            baseline_measurement = measure_objective(
                baseline_dev, active_objective
            )
            if baseline_measurement.status != "measured":
                return finish_optimization(
                    baseline_dev=baseline_dev,
                    accepted=False,
                    stop_reason="tune objective is not completely measurable on baseline dev",
                    output_dir=optimization_dir,
                    limitations=(
                        "Missing objective values are not interpreted as zero.",
                    ),
                )

        configured_rounds = policy.max_rounds
        rounds = configured_rounds if max_rounds is None else min(configured_rounds, max_rounds)
        if rounds <= 0:
            raise OrchestrationError("max_rounds must be positive")
        patch_policy = SkillPatchPolicy(
            max_added_lines=(
                policy.max_added_lines
                if policy.max_added_lines is not None
                else 30
            ),
            forbid_case_literals=policy.forbid_case_literals,
        )
        forbidden_literals = self._forbidden_literals(pack)
        proposal_contract = getattr(candidate_optimizer, "proposal_contract", None)
        supported_modes = getattr(candidate_optimizer, "supported_modes", None)
        if (
            active_mode == ImprovementMode.TUNE
            and supported_modes is not None
            and ImprovementMode.TUNE.value not in supported_modes
        ):
            raise OrchestrationError("optimizer does not support tune mode")
        if proposal_contract == CANDIDATE_PATCH_OPTIMIZER_CONTRACT:
            bridge = candidate_optimizer
        elif proposal_contract in (
            SKILL_MARKDOWN_GENERATOR_CONTRACT,
            SKILL_MARKDOWN_IMPROVER_CONTRACT,
        ):
            if (
                active_mode == ImprovementMode.TUNE
                and proposal_contract == SKILL_MARKDOWN_GENERATOR_CONTRACT
                and ImprovementMode.TUNE.value
                not in (supported_modes or ())
            ):
                raise OrchestrationError(
                    "skill-markdown-generator-v1 is repair-only"
                )
            bridge = SkillOptimizerBridge(
                candidate_optimizer,
                candidate_root,
                patch_policy,
                forbidden_literals,
            )
        else:
            raise OrchestrationError(
                "optimizer must declare a supported proposal_contract"
            )
        constraints = PatchConstraints(
            allowed_paths=tuple(policy.allowed_paths),
            max_added_lines=policy.max_added_lines,
            max_candidates=policy.max_candidate_snapshots,
            metadata={
                "candidate_root": str(candidate_root),
                "optimizer_params": dict(policy.params),
                "patchable_components": tuple(policy.patchable_components),
                "improvement_mode": active_mode.value,
                "goal": active_goal,
                "objective": (
                    self._objective_payload(active_objective)
                    if active_objective is not None
                    else None
                ),
            },
        )
        frontier = [(base_snapshot, baseline_dev)]
        trials = []  # type: List[CandidateTrial]
        frozen_candidates = {}  # type: Dict[str, SubjectSnapshot]
        seen_hashes = {base_snapshot.content_hash}
        candidate_id_hashes = {}  # type: dict
        budget_stop = None
        protocol_stop = None

        for round_index in range(1, rounds + 1):
            next_frontier = []
            for parent_snapshot, parent_run in frontier:
                if budget_stop is not None or protocol_stop is not None:
                    break
                if self._has_non_skill_failure(parent_run):
                    continue
                parent_evidence = self._improvement_evidence(
                    active_mode,
                    parent_run,
                    active_goal,
                    active_objective,
                )
                if not parent_evidence:
                    next_frontier.append((parent_snapshot, parent_run))
                    continue
                for _ in range(policy.beam_width):
                    if len(trials) >= policy.max_candidate_snapshots:
                        break
                    try:
                        ledger.ensure_available()
                        proposal_attempt_count += 1
                        patches = bridge.propose(
                            parent_snapshot,
                            parent_evidence,
                            constraints,
                        )
                        if inspect.isawaitable(patches):
                            patches = await patches
                    except _BudgetExceeded as exc:
                        budget_stop = str(exc)
                        break
                    except CandidateRejected as exc:
                        rejected_proposal_count += 1
                        try:
                            ledger.consume_usage(exc.usage)
                        except _BudgetExceeded as budget_exc:
                            budget_stop = str(budget_exc)
                            break
                        continue
                    for candidate_patch in tuple(patches):
                        if len(trials) >= policy.max_candidate_snapshots:
                            break
                        try:
                            candidate_usage = self._candidate_patch_usage(
                                candidate_patch
                            )
                            ledger.consume_usage(candidate_usage)
                        except _InvalidUsage as exc:
                            rejected_proposal_count += 1
                            protocol_stop = (
                                "optimizer protocol error: invalid candidate accounting: %s"
                                % exc
                            )
                            break
                        except _BudgetExceeded as exc:
                            rejected_proposal_count += 1
                            budget_stop = str(exc)
                            break
                        except OrchestrationError as exc:
                            rejected_proposal_count += 1
                            protocol_stop = "optimizer protocol error: %s" % exc
                            break
                        try:
                            (
                                candidate_id,
                                candidate_path,
                                candidate_snapshot,
                                candidate_hash,
                                validated_usage,
                            ) = self._validate_candidate_patch(
                                candidate_patch,
                                parent_snapshot,
                                candidate_root,
                                adapter,
                                policy.allowed_paths,
                                policy.max_added_lines,
                                forbidden_literals if policy.forbid_case_literals else (),
                                pack.manifest.subject_contract.entrypoint,
                                subject_params,
                            )
                        except OrchestrationError as exc:
                            rejected_proposal_count += 1
                            protocol_stop = "optimizer protocol error: %s" % exc
                            break
                        if dict(validated_usage) != dict(candidate_usage):
                            raise OrchestrationError(
                                "candidate usage changed during validation"
                            )
                        previous_candidate_hash = candidate_id_hashes.get(candidate_id)
                        if (
                            previous_candidate_hash is not None
                            and previous_candidate_hash != candidate_hash
                        ):
                            raise OrchestrationError(
                                "optimizer reused a candidate id for different content: %s"
                                % candidate_id
                            )
                        if previous_candidate_hash is None:
                            candidate_id_hashes[candidate_id] = candidate_hash
                        if candidate_hash in seen_hashes:
                            duplicate_proposal_count += 1
                            continue
                        seen_hashes.add(candidate_hash)
                        candidate_snapshot = with_variant(candidate_snapshot, "candidate")
                        frozen_candidates[candidate_hash] = candidate_snapshot
                        dev_run = await self._evaluate(
                            pack,
                            candidate_snapshot,
                            ("dev",),
                            "candidate-dev-r%d" % round_index,
                            ledger=ledger,
                        )
                        comparison = _comparison(baseline_dev, dev_run)
                        objective_comparison = None
                        if active_mode == ImprovementMode.REPAIR:
                            improved = (
                                comparison.paired_uplift is not None
                                and comparison.paired_uplift > 0
                                and not comparison.hard_regressions
                                and not self._has_non_skill_failure(dev_run)
                            )
                            promoted = improved and dev_run.passed
                            if promoted:
                                reason = None
                            elif improved:
                                reason = "dev hard failures remain; candidate may continue to another dev-only round"
                            else:
                                reason = "no dev uplift, a hard regression, or a non-Skill failure"
                        else:
                            objective_comparison = compare_objective(
                                baseline_dev, dev_run, active_objective
                            )
                            parent_objective = compare_objective(
                                parent_run, dev_run, active_objective
                            )
                            parent_hard = _comparison(parent_run, dev_run)
                            hard_safe = (
                                dev_run.passed
                                and not comparison.hard_regressions
                                and not parent_hard.hard_regressions
                                and not self._has_non_skill_failure(dev_run)
                            )
                            improved = (
                                hard_safe
                                and parent_objective.evaluable
                                and parent_objective.improvement is not None
                                and parent_objective.improvement > 0
                                and not parent_objective.case_regressions
                            )
                            promoted = improved and objective_comparison.passed
                            if promoted:
                                reason = None
                            elif not hard_safe:
                                reason = "tune candidate violated a hard gate or introduced a hard regression"
                            elif not objective_comparison.evaluable:
                                reason = "tune objective was not completely measurable"
                            elif objective_comparison.case_regressions:
                                reason = "tune candidate exceeded the per-case objective regression limit"
                            elif not improved:
                                reason = "tune candidate did not improve its parent"
                            else:
                                reason = "tune candidate did not reach the objective delta or target"
                        trial = CandidateTrial(
                            round_index=round_index,
                            candidate_id=candidate_id,
                            candidate_path=candidate_path,
                            candidate_hash=candidate_hash,
                            parent_hash=candidate_patch.base_hash,
                            patch=candidate_patch.unified_diff,
                            rationale=candidate_patch.rationale,
                            dev_run=dev_run,
                            promoted_from_dev=promoted,
                            rejection_reason=reason,
                            usage=candidate_usage,
                            objective_comparison=objective_comparison,
                        )
                        trials.append(trial)
                        if improved:
                            next_frontier.append((candidate_snapshot, dev_run))
                    if budget_stop is not None or protocol_stop is not None:
                        break
            if budget_stop is not None or protocol_stop is not None:
                break
            if not next_frontier or len(trials) >= policy.max_candidate_snapshots:
                break
            if active_mode == ImprovementMode.REPAIR:
                next_frontier.sort(
                    key=lambda item: item[1].hard_pass_rate, reverse=True
                )
            else:
                next_frontier.sort(
                    key=lambda item: self._objective_rank(
                        item[1], active_objective
                    ),
                    reverse=True,
                )
            frontier = next_frontier[: policy.beam_width]
            if active_mode == ImprovementMode.REPAIR and frontier and frontier[0][1].passed:
                break
            if active_mode == ImprovementMode.TUNE and any(
                item.promoted_from_dev for item in trials
            ):
                break

        if budget_stop is not None:
            return finish_optimization(
                baseline_dev=baseline_dev,
                trials=tuple(trials),
                accepted=False,
                stop_reason="optimization budget exhausted: %s" % budget_stop,
                output_dir=optimization_dir,
                limitations=(
                    "The optimization stopped before running another candidate gate.",
                ),
            )

        if protocol_stop is not None:
            return finish_optimization(
                baseline_dev=baseline_dev,
                trials=tuple(trials),
                accepted=False,
                stop_reason=protocol_stop,
                output_dir=optimization_dir,
                limitations=(
                    "The optimizer returned a proposal that violated the candidate-patch contract.",
                ),
            )

        promoted_trials = [item for item in trials if item.promoted_from_dev]
        if active_mode == ImprovementMode.REPAIR:
            promoted_trials.sort(
                key=lambda item: (item.dev_run.hard_pass_rate, -item.round_index),
                reverse=True,
            )
        else:
            promoted_trials.sort(
                key=lambda item: (
                    item.objective_comparison.improvement
                    if item.objective_comparison is not None
                    and item.objective_comparison.improvement is not None
                    else float("-inf"),
                    -item.round_index,
                ),
                reverse=True,
            )
        if not promoted_trials:
            return finish_optimization(
                baseline_dev=baseline_dev,
                trials=tuple(trials),
                accepted=False,
                stop_reason=(
                    "no candidate improved dev without a hard regression"
                    if active_mode == ImprovementMode.REPAIR
                    else "no candidate met the tune objective without a hard regression"
                ),
                output_dir=optimization_dir,
                limitations=("Optimizer received dev evidence only.",),
            )

        baseline_validation = self._optional_run_placeholder(pack, "validation")
        if baseline_validation:
            baseline_validation = await self._evaluate(
                pack,
                base_snapshot,
                ("validation",),
                "baseline-validation",
                ledger=ledger,
            )
            if self._has_non_skill_failure(baseline_validation):
                return finish_optimization(
                    baseline_dev=baseline_dev,
                    trials=tuple(trials),
                    baseline_validation=baseline_validation,
                    accepted=False,
                    stop_reason=(
                        "baseline validation contains runtime, evaluator, or "
                        "missing-evidence failures; promotion was not attempted"
                    ),
                    output_dir=optimization_dir,
                    limitations=(
                        "Validation infrastructure failures cannot authorize or reject a Skill change.",
                    ),
                )
            if active_mode == ImprovementMode.TUNE and not baseline_validation.passed:
                return finish_optimization(
                    baseline_dev=baseline_dev,
                    trials=tuple(trials),
                    baseline_validation=baseline_validation,
                    accepted=False,
                    stop_reason=(
                        "baseline is not tune-eligible because validation hard gates do not all pass; run repair first"
                    ),
                    output_dir=optimization_dir,
                    limitations=(
                        "Tune mode never uses a candidate to hide a pre-existing hard failure on a promotion split.",
                    ),
                )
        selected_trial = None
        candidate_validation = None
        selected_validation_objective = None
        validation_attempts = []  # type: List[ValidationAttempt]
        for trial in promoted_trials[: policy.beam_width]:
            candidate_snapshot = self._frozen_trial_snapshot(
                trial, frozen_candidates, adapter, subject_params
            )
            if baseline_validation is None:
                selected_trial = trial
                break
            validation = await self._evaluate(
                pack,
                candidate_snapshot,
                ("validation",),
                "candidate-validation",
                ledger=ledger,
            )
            if self._has_non_skill_failure(validation):
                validation_attempts.append(
                    ValidationAttempt(
                        candidate_id=trial.candidate_id,
                        candidate_hash=trial.candidate_hash,
                        run=validation,
                        passed_gate=False,
                    )
                )
                return finish_optimization(
                    baseline_dev=baseline_dev,
                    trials=tuple(trials),
                    baseline_validation=baseline_validation,
                    candidate_validation=validation,
                    validation_attempts=tuple(validation_attempts),
                    accepted=False,
                    stop_reason=(
                        "candidate validation contains runtime, evaluator, or "
                        "missing-evidence failures; the experiment stopped"
                    ),
                    output_dir=optimization_dir,
                    limitations=(
                        "Validation infrastructure failures are not candidate quality failures.",
                    ),
                )
            gate = _comparison(baseline_validation, validation)
            validation_objective = (
                compare_objective(
                    baseline_validation, validation, active_objective
                )
                if active_mode == ImprovementMode.TUNE
                else None
            )
            passed_validation = (
                validation.passed
                and not gate.hard_regressions
                and (
                    validation_objective is None
                    or validation_objective.passed
                )
            )
            validation_attempts.append(
                ValidationAttempt(
                    candidate_id=trial.candidate_id,
                    candidate_hash=trial.candidate_hash,
                    run=validation,
                    passed_gate=passed_validation,
                    hard_regressions=gate.hard_regressions,
                    objective_comparison=validation_objective,
                )
            )
            if passed_validation:
                selected_trial = trial
                candidate_validation = validation
                selected_validation_objective = validation_objective
                break
            if candidate_validation is None:
                candidate_validation = validation

        if selected_trial is None:
            return finish_optimization(
                baseline_dev=baseline_dev,
                trials=tuple(trials),
                baseline_validation=baseline_validation,
                candidate_validation=candidate_validation,
                validation_attempts=tuple(validation_attempts),
                accepted=False,
                stop_reason="all dev-promoted candidates failed the validation gate",
                output_dir=optimization_dir,
                limitations=(
                    "Validation results were used only for promotion and were not returned to the optimizer.",
                ),
            )

        candidate_snapshot = self._frozen_trial_snapshot(
            selected_trial, frozen_candidates, adapter, subject_params
        )
        baseline_holdout = None
        candidate_holdout = None
        holdout_batch_count = 0
        holdout_pair_count = 0
        holdout_objective = None
        accepted = True
        stop_reason = (
            "candidate passed dev and validation"
            if baseline_validation is not None
            else "candidate passed dev; no validation split is declared"
        )
        if self._optional_run_placeholder(pack, "holdout"):
            if active_mode == ImprovementMode.TUNE:
                baseline_holdout = await self._evaluate(
                    pack,
                    base_snapshot,
                    ("holdout",),
                    "baseline-holdout",
                    allow_holdout=True,
                    ledger=ledger,
                )
                if self._has_non_skill_failure(baseline_holdout):
                    return finish_optimization(
                        baseline_dev=baseline_dev,
                        trials=tuple(trials),
                        validation_attempts=tuple(validation_attempts),
                        baseline_validation=baseline_validation,
                        candidate_validation=candidate_validation,
                        baseline_holdout=baseline_holdout,
                        accepted=False,
                        stop_reason=(
                            "baseline holdout contains runtime, evaluator, or missing-evidence failures; tune quality was not determined"
                        ),
                        output_dir=optimization_dir,
                        limitations=(
                            "Holdout results were not returned to the optimizer.",
                        ),
                    )
                if not baseline_holdout.passed:
                    return finish_optimization(
                        baseline_dev=baseline_dev,
                        trials=tuple(trials),
                        validation_attempts=tuple(validation_attempts),
                        baseline_validation=baseline_validation,
                        candidate_validation=candidate_validation,
                        baseline_holdout=baseline_holdout,
                        accepted=False,
                        stop_reason=(
                            "baseline is not tune-eligible because holdout hard gates do not all pass; run repair first"
                        ),
                        output_dir=optimization_dir,
                        limitations=(
                            "Tune mode never uses a candidate to hide a pre-existing hard failure on the holdout split.",
                            "Holdout results were not returned to the optimizer.",
                        ),
                    )
            candidate_holdout = await self._evaluate(
                pack,
                candidate_snapshot,
                ("holdout",),
                "candidate-holdout",
                allow_holdout=True,
                ledger=ledger,
            )
            holdout_batch_count = 1
            if active_mode == ImprovementMode.TUNE:
                holdout_pair_count = 1
            if self._has_non_skill_failure(candidate_holdout):
                accepted = False
                stop_reason = (
                    "final holdout contains runtime, evaluator, or missing-evidence "
                    "failures; candidate quality was not determined"
                )
            else:
                if active_mode == ImprovementMode.TUNE:
                    holdout_gate = _comparison(
                        baseline_holdout, candidate_holdout
                    )
                    holdout_objective = compare_objective(
                        baseline_holdout,
                        candidate_holdout,
                        active_objective,
                    )
                    accepted = (
                        candidate_holdout.passed
                        and not holdout_gate.hard_regressions
                        and holdout_objective.passed
                    )
                else:
                    accepted = candidate_holdout.passed
                passed_gates = (
                    "dev, validation, and holdout"
                    if baseline_validation is not None
                    else "dev and holdout"
                )
                stop_reason = (
                    (
                        "candidate passed %s and the tune objective" % passed_gates
                        if active_mode == ImprovementMode.TUNE
                        else "candidate passed %s" % passed_gates
                    )
                    if accepted
                    else (
                        "candidate failed the paired holdout tune gate"
                        if active_mode == ImprovementMode.TUNE
                        else "candidate failed the final holdout gate"
                    )
                )
        limitations = [
            (
                "Optimizer received dev failure evidence only."
                if active_mode == ImprovementMode.REPAIR
                else "Optimizer received only the dev tuning objective and measurements."
            ),
            "Acceptance applies to the built-in runtime profile, not every Agent platform.",
            "A FakeRuntime acceptance is a deterministic simulation, not a model benchmark result.",
        ]
        if active_mode == ImprovementMode.TUNE:
            limitations.append(
                "Objective comparisons use one paired run per scenario and do not establish statistical significance."
            )
        if baseline_validation is not None:
            limitations.append(
                "Validation was a promotion-only gate and was not returned to the optimizer."
            )
        else:
            limitations.append("No validation split was declared for this EvalPack.")
        if candidate_holdout is not None:
            limitations.append(
                (
                    "Holdout ran once as a paired baseline/candidate tune gate."
                    if active_mode == ImprovementMode.TUNE
                    else "Holdout ran once for the selected candidate."
                )
            )
        else:
            limitations.append("No holdout split was declared for this EvalPack.")
        selected_candidate_path = self._materialize_selected_candidate(
            adapter,
            candidate_snapshot,
            optimization_dir / "selected-candidate",
            subject_params,
        )
        return finish_optimization(
            baseline_dev=baseline_dev,
            trials=tuple(trials),
            validation_attempts=tuple(validation_attempts),
            baseline_validation=baseline_validation,
            candidate_validation=candidate_validation,
            baseline_holdout=baseline_holdout,
            candidate_holdout=candidate_holdout,
            holdout_batch_count=holdout_batch_count,
            holdout_pair_count=holdout_pair_count,
            selected_candidate_id=selected_trial.candidate_id,
            selected_candidate_path=selected_candidate_path,
            selected_candidate_hash=candidate_snapshot.content_hash,
            accepted=accepted,
            stop_reason=stop_reason,
            output_dir=optimization_dir,
            limitations=tuple(limitations),
            dev_objective=selected_trial.objective_comparison,
            validation_objective=selected_validation_objective,
            holdout_objective=holdout_objective,
        )

    def _check_runtime_capabilities(
        self, pack: FrozenEvalPack, driver: Any, scenarios: Sequence[FrozenScenario]
    ) -> None:
        required = set(pack.manifest.driver.required_runtime_capabilities)
        for scenario in scenarios:
            required.update(
                driver.required_capabilities(
                    self._driver_scenario_view(scenario, self.runtime_profile)
                )
            )
        missing = self.runtime_profile.capabilities.missing(required)
        if missing:
            raise OrchestrationError(
                "runtime %s lacks required capabilities: %s"
                % (getattr(self.runtime, "id", "runtime"), ", ".join(sorted(missing)))
            )

    @staticmethod
    def _driver_scenario_view(
        scenario: FrozenScenario, profile: RuntimeProfile
    ) -> DriverScenarioView:
        metadata = {}
        simulated = profile.adapter == "fake" or bool(
            profile.metadata.get("simulated", False)
        )
        if simulated and "fake_runtime" in scenario.scenario.metadata:
            metadata["fake_runtime"] = scenario.scenario.metadata["fake_runtime"]
        return DriverScenarioView(
            id=scenario.id,
            split=scenario.split,
            prompt=scenario.prompt,
            fixtures=scenario.fixtures,
            timeout_seconds=scenario.timeout_seconds,
            tags=scenario.scenario.tags,
            metadata=metadata,
        )

    def _capture_runtime_profile(self) -> RuntimeProfile:
        runtime_id = str(getattr(self.runtime, "id", type(self.runtime).__name__))
        capabilities = getattr(self.runtime, "capabilities", ())
        declared = getattr(self.runtime, "profile", None)
        if callable(declared):
            declared = declared()
        if declared is None:
            profile = RuntimeProfile(
                adapter=runtime_id,
                capabilities=capabilities,
                metadata={
                    "profile_complete": False,
                    "protocol_version": "aceval.runtime/v1",
                },
            )
        elif isinstance(declared, RuntimeProfile):
            profile = declared
        else:
            raise ValueError("runtime.profile must be a RuntimeProfile")
        if profile.adapter != runtime_id:
            raise ValueError("runtime profile adapter does not match runtime id")
        runtime_capabilities = frozenset(getattr(capabilities, "values", capabilities))
        if profile.capabilities.values != runtime_capabilities:
            raise ValueError(
                "runtime profile capabilities do not match runtime capabilities"
            )
        return profile

    async def _evaluate_scenario(
        self,
        pack: FrozenEvalPack,
        scenario: FrozenScenario,
        subject: SubjectSnapshot,
        driver: Any,
        profile: RuntimeProfile,
        run_dir: Path,
        frozen_pack_root: Path,
        ledger: _BudgetLedger,
    ) -> ScenarioEvaluation:
        started = time.monotonic()
        prepared = None
        observation = None
        grades = []  # type: List[GradeResult]
        error = None
        scenario_run_id = "%s-%s" % (run_dir.name, scenario.id)
        scenario_budget = ledger.remaining(scenario.timeout_seconds)
        case_directory = "case-%s" % hashlib.sha256(
            scenario.id.encode("utf-8")
        ).hexdigest()[:16]
        fixture_root = self._materialize_scenario_fixtures(
            frozen_pack_root,
            scenario,
            run_dir / "case-inputs" / case_directory,
        )
        driver_context = RunContext(
            run_id=scenario_run_id,
            pack_hash=pack.pack_hash,
            subject_hash=subject.content_hash,
            runtime_profile=profile,
            budget=scenario_budget,
            metadata={
                "driver_params": dict(pack.manifest.driver.params),
                "fixture_root": str(fixture_root),
                "work_root": str(run_dir / "workspaces"),
                "artifact_paths": self._artifact_paths(pack, scenario),
            },
        )
        runtime_context = RunContext(
            run_id=scenario_run_id,
            pack_hash=pack.pack_hash,
            subject_hash=subject.content_hash,
            runtime_profile=profile,
            budget=scenario_budget,
            metadata={"scenario_id": scenario.id},
        )
        driver_scenario = self._driver_scenario_view(scenario, profile)
        try:
            prepared = await driver.prepare(driver_scenario, driver_context)
            try:
                runtime_result = await self.runtime.execute(
                    prepared, subject, runtime_context
                )
            except Exception as exc:
                partial_usage = getattr(exc, "usage", {})
                if not isinstance(partial_usage, Mapping):
                    partial_usage = {}
                partial_trace = getattr(exc, "trace", ())
                if isinstance(partial_trace, Mapping):
                    partial_trace = (partial_trace,)
                elif isinstance(partial_trace, Sequence) and not isinstance(
                    partial_trace, (str, bytes)
                ):
                    partial_trace = tuple(partial_trace)
                else:
                    partial_trace = ()
                runtime_result = RuntimeResult(
                    final_output=getattr(exc, "final_output", None),
                    trace=partial_trace,
                    error="runtime_exception: %s" % exc,
                    usage=dict(partial_usage),
                    metadata={
                        "runtime_adapter_exception": True,
                        "partial_accounting": bool(partial_usage or partial_trace),
                    },
                )
            if (
                scenario_budget.max_wall_time_seconds is not None
                and time.monotonic() - started
                >= scenario_budget.max_wall_time_seconds
                and not runtime_result.error
            ):
                runtime_result = replace(
                    runtime_result,
                    error="scenario wall-time budget exceeded",
                )
            try:
                observation = await driver.collect(prepared, runtime_result)
            except Exception as exc:
                error = "collect_error: %s" % exc
                observation = RunObservation(
                    output=runtime_result.final_output,
                    trace=runtime_result.trace,
                    artifacts=runtime_result.artifacts,
                    error=error,
                    usage=runtime_result.usage,
                    metadata=runtime_result.metadata,
                )
            if observation.error:
                error = observation.error
                grades = self._not_evaluable_grades(pack, scenario, observation.error)
            else:
                grades = await self._grade(
                    pack, scenario, observation, frozen_pack_root
                )
        except Exception as exc:
            error = "lifecycle_error: %s" % exc
            grades = self._not_evaluable_grades(pack, scenario, error)
        finally:
            if prepared is not None:
                try:
                    await driver.cleanup(prepared)
                except Exception as exc:
                    cleanup_error = "cleanup_error: %s" % exc
                    error = "%s; %s" % (error, cleanup_error) if error else cleanup_error
        duration = time.monotonic() - started
        if (
            scenario_budget.max_wall_time_seconds is not None
            and duration >= scenario_budget.max_wall_time_seconds
        ):
            timeout_error = "scenario wall-time budget exceeded"
            if timeout_error not in str(error or ""):
                error = "%s; %s" % (error, timeout_error) if error else timeout_error
            grades = self._not_evaluable_grades(pack, scenario, timeout_error)
        status = _overall_status(grades, error)
        return ScenarioEvaluation(
            scenario_id=scenario.id,
            split=scenario.split,
            status=status,
            grades=tuple(grades),
            observation=observation,
            duration_seconds=duration,
            error=error,
        )

    async def _grade(
        self,
        pack: FrozenEvalPack,
        scenario: FrozenScenario,
        observation: RunObservation,
        frozen_pack_root: Path,
    ) -> List[GradeResult]:
        results = []
        oracle = scenario.oracle or FrozenOracle()
        for grader_id in scenario.grader_ids:
            spec = pack.manifest.grader_by_id(grader_id)
            grader = self.registry.grader(spec.type)
            params = dict(spec.params)
            params.update(scenario.scenario.grader_params.get(grader_id, {}))
            params["pack_root"] = str(frozen_pack_root)
            params["resources"] = pack.resources
            params["hard"] = spec.hard
            try:
                value = grader.evaluate(observation, oracle, params)
                if inspect.isawaitable(value):
                    value = await value
                if not isinstance(value, GradeResult):
                    raise TypeError("grader returned %s instead of GradeResult" % type(value).__name__)
                value = replace(
                    value,
                    grader_id=grader_id,
                    hard=spec.hard,
                    version=value.version or str(getattr(grader, "version", "")),
                )
            except Exception as exc:
                value = GradeResult(
                    grader_id=grader_id,
                    status=GradeStatus.ERROR,
                    hard=spec.hard,
                    version=str(getattr(grader, "version", "")),
                    message="grader_error: %s" % exc,
                )
            results.append(value)
        return results

    def _not_evaluable_grades(
        self, pack: FrozenEvalPack, scenario: FrozenScenario, reason: str
    ) -> List[GradeResult]:
        return [
            GradeResult(
                grader_id=grader_id,
                status=GradeStatus.NOT_EVALUABLE,
                hard=pack.manifest.grader_by_id(grader_id).hard,
                message=reason,
                missing=("runtime_observation",),
            )
            for grader_id in scenario.grader_ids
        ]

    @staticmethod
    def _artifact_paths(pack: FrozenEvalPack, scenario: FrozenScenario) -> Tuple[str, ...]:
        values = []
        metadata_paths = scenario.scenario.metadata.get("artifact_paths", ())
        if isinstance(metadata_paths, str):
            values.append(metadata_paths)
        else:
            values.extend(metadata_paths)
        for grader_id in scenario.grader_ids:
            spec = pack.manifest.grader_by_id(grader_id)
            params = dict(spec.params)
            params.update(scenario.scenario.grader_params.get(grader_id, {}))
            if params.get("artifact_path"):
                values.append(str(params["artifact_path"]))
            if spec.type == "artifact_exists" and params.get("path"):
                values.append(str(params["path"]))
        return tuple(dict.fromkeys(values))

    @classmethod
    def _improvement_evidence(
        cls,
        mode: ImprovementMode,
        run: EvalRun,
        goal: str,
        objective: Optional[ObjectiveSpec],
    ) -> Tuple[Any, ...]:
        if mode == ImprovementMode.REPAIR:
            return cls._failure_evidence(run)
        if objective is None:
            return ()
        measurement = measure_objective(run, objective)
        if measurement.status != "measured" or measurement.value is None:
            return ()
        return (
            TuneEvidence(
                mode=mode.value,
                goal=goal,
                objective=cls._objective_payload(objective),
                baseline_value=measurement.value,
                scenario_values=measurement.scenario_values,
            ),
        )

    @staticmethod
    def _objective_rank(run: EvalRun, objective: ObjectiveSpec) -> float:
        measurement = measure_objective(run, objective)
        if measurement.status != "measured" or measurement.value is None:
            return float("-inf")
        return (
            measurement.value
            if objective.direction.value == "maximize"
            else -measurement.value
        )

    @staticmethod
    def _objective_payload(objective: ObjectiveSpec) -> Mapping[str, Any]:
        return {
            "id": objective.id,
            "source": {
                "type": objective.source.type,
                "grader_id": objective.source.grader_id,
                "key": objective.source.key,
            },
            "direction": objective.direction.value,
            "aggregation": objective.aggregation,
            "min_delta": objective.min_delta,
            "target": objective.target,
            "max_case_regression": objective.max_case_regression,
        }

    @staticmethod
    def _failure_evidence(run: EvalRun) -> Tuple[FailureEvidence, ...]:
        failures = []
        for scenario in run.scenarios:
            for grade in scenario.grades:
                if grade.hard and grade.status == GradeStatus.FAIL:
                    evidence = []
                    for item in grade.evidence:
                        if isinstance(item, Mapping):
                            evidence.append(dict(item))
                        else:
                            evidence.append({"value": str(item)})
                    failures.append(
                        FailureEvidence(
                            scenario_id=scenario.scenario_id,
                            grader_id=grade.grader_id,
                            summary=grade.message or grade.status.value,
                            evidence=tuple(evidence),
                        )
                    )
        return tuple(failures)

    @staticmethod
    def _forbidden_literals(pack: FrozenEvalPack) -> Tuple[str, ...]:
        values = []
        for scenario in pack.scenarios:
            if scenario.split != "dev":
                continue
            values.append(scenario.id)
            declared = scenario.scenario.metadata.get("forbidden_literals", ())
            if isinstance(declared, str):
                values.append(declared)
            else:
                values.extend(str(item) for item in declared)
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _optional_run_placeholder(pack: FrozenEvalPack, split: str) -> bool:
        return any(item.split == split for item in pack.scenarios)

    @staticmethod
    def _has_non_skill_failure(run: EvalRun) -> bool:
        for scenario in run.scenarios:
            if scenario.error:
                return True
            if any(
                grade.status in (GradeStatus.ERROR, GradeStatus.NOT_EVALUABLE)
                for grade in scenario.grades
            ):
                return True
        return False

    def _budget_failure(
        self, pack: FrozenEvalPack, scenario: FrozenScenario, reason: str
    ) -> ScenarioEvaluation:
        return ScenarioEvaluation(
            scenario_id=scenario.id,
            split=scenario.split,
            status=GradeStatus.ERROR,
            grades=tuple(self._not_evaluable_grades(pack, scenario, reason)),
            error=reason,
        )

    @staticmethod
    def _split_declared(pack: FrozenEvalPack, split: str) -> bool:
        suite = pack.manifest.suite
        if split == "dev":
            return suite.dev is not None
        if split == "validation":
            return suite.validation is not None or suite.validation_ref is not None
        if split == "holdout":
            return suite.holdout is not None or suite.holdout_ref is not None
        return False

    @classmethod
    def _ensure_splits_resolved(
        cls, pack: FrozenEvalPack, splits: Sequence[str]
    ) -> None:
        for split in splits:
            if cls._split_declared(pack, split) and not any(
                item.split == split for item in pack.scenarios
            ):
                raise OrchestrationError(
                    "%s suite is declared but unresolved; no external suite resolver is configured"
                    % split
                )

    def _verify_pack_integrity(self, pack: FrozenEvalPack) -> None:
        current = {}
        try:
            for entry in sorted(pack.root.rglob("*")):
                if entry.is_symlink():
                    raise OrchestrationError(
                        "EvalPack changed after freeze: symlink appeared at %s"
                        % entry.relative_to(pack.root)
                    )
                if entry.is_file():
                    current[entry.relative_to(pack.root).as_posix()] = hashlib.sha256(
                        entry.read_bytes()
                    ).hexdigest()
        except OSError as exc:
            raise OrchestrationError(
                "cannot verify frozen EvalPack contents: %s" % exc
            ) from exc
        if current != dict(pack.file_hashes):
            raise OrchestrationError(
                "EvalPack files changed after load; reload the Pack before executing"
            )
        current_pack_hash = hashlib.sha256(
            json.dumps(
                current,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if current_pack_hash != pack.pack_hash:
            raise OrchestrationError("EvalPack hash does not match its frozen file manifest")
        try:
            reloaded = EvalPackLoader(self.registry).load(pack.root)
        except (PackError, OSError, ValueError) as exc:
            raise OrchestrationError(
                "cannot reconstruct EvalPack semantic snapshot: %s" % exc
            ) from exc
        if reloaded != pack:
            raise OrchestrationError(
                "in-memory EvalPack differs from the semantic snapshot reconstructed from disk"
            )

    def _ensure_output_outside_pack(self, pack: FrozenEvalPack) -> None:
        try:
            self.output_root.relative_to(pack.root)
        except ValueError:
            return
        raise OrchestrationError("output_root must not be inside the EvalPack root")

    @staticmethod
    def _materialize_pack_snapshot(pack: FrozenEvalPack, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=False)
        for relative, expected_hash in sorted(pack.file_hashes.items()):
            relative_path = Path(relative)
            if (
                relative_path.is_absolute()
                or not relative_path.parts
                or ".." in relative_path.parts
            ):
                raise OrchestrationError(
                    "frozen EvalPack contains an unsafe file path: %s" % relative
                )
            source = pack.root.joinpath(*relative_path.parts)
            if source.is_symlink() or not source.is_file():
                raise OrchestrationError(
                    "EvalPack changed while creating the run snapshot: %s" % relative
                )
            try:
                content = source.read_bytes()
            except OSError as exc:
                raise OrchestrationError(
                    "cannot freeze EvalPack file %s: %s" % (relative, exc)
                ) from exc
            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash != expected_hash:
                raise OrchestrationError(
                    "EvalPack changed while creating the run snapshot: %s" % relative
                )
            target = destination.joinpath(*relative_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o444)
        return destination

    @staticmethod
    def _materialize_scenario_fixtures(
        frozen_pack_root: Path,
        scenario: FrozenScenario,
        destination: Path,
    ) -> Path:
        destination.mkdir(parents=True, exist_ok=False)
        for reference in scenario.fixtures:
            relative = Path(reference)
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise OrchestrationError(
                    "frozen Scenario contains an unsafe fixture path: %s" % reference
                )
            source = frozen_pack_root.joinpath(*relative.parts)
            if source.is_symlink() or not source.exists():
                raise OrchestrationError(
                    "frozen Scenario fixture is unavailable: %s" % reference
                )
            sources = (source,) if source.is_file() else tuple(
                item for item in sorted(source.rglob("*")) if item.is_file()
            )
            if source.is_dir():
                destination.joinpath(*relative.parts).mkdir(
                    parents=True, exist_ok=True
                )
            for item in sources:
                if item.is_symlink():
                    raise OrchestrationError(
                        "frozen Scenario fixture contains a symlink: %s" % reference
                    )
                item_relative = item.relative_to(frozen_pack_root)
                target = destination.joinpath(*item_relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item.read_bytes())
                target.chmod(0o444)
        return destination

    @staticmethod
    def _candidate_patch_usage(patch: Any) -> Mapping[str, Any]:
        if not isinstance(patch, CandidatePatch):
            return {}
        if not isinstance(patch.metadata, Mapping):
            raise OrchestrationError("candidate metadata must be an object")
        usage = patch.metadata.get("usage", {})
        if not isinstance(usage, Mapping):
            raise OrchestrationError("candidate usage must be an object")
        return dict(usage)

    @staticmethod
    def _validate_candidate_patch(
        patch: CandidatePatch,
        parent: SubjectSnapshot,
        candidate_root: Path,
        adapter: Any,
        policy_paths: Sequence[str],
        max_added_lines: Optional[int],
        forbidden_literals: Sequence[str],
        entrypoint: str,
        subject_params: Mapping[str, Any],
    ) -> Tuple[str, Path, SubjectSnapshot, str, Mapping[str, Any]]:
        if not isinstance(patch, CandidatePatch):
            raise OrchestrationError("optimizer returned a non-CandidatePatch value")
        if patch.base_hash != parent.content_hash:
            raise OrchestrationError("candidate base hash does not match its parent snapshot")
        allowed = set(policy_paths)
        declared_paths = set(patch.allowed_paths)
        if not allowed or not declared_paths or not declared_paths.issubset(allowed):
            raise OrchestrationError("candidate patch exceeds policy.allowed_paths")
        if not patch.unified_diff.strip():
            raise OrchestrationError("candidate patch is empty")
        if patch.patch_hash is not None:
            actual_patch_hash = hashlib.sha256(
                patch.unified_diff.encode("utf-8")
            ).hexdigest()
            if actual_patch_hash != patch.patch_hash:
                raise OrchestrationError("candidate patch hash mismatch")
        touched = set()
        for line in patch.unified_diff.splitlines():
            if line.startswith("--- a/") or line.startswith("+++ b/"):
                touched.add(line[6:])
        if (
            not touched
            or not touched.issubset(allowed)
            or not touched.issubset(declared_paths)
        ):
            raise OrchestrationError("candidate diff touches an undeclared path")
        added_lines = sum(
            line.startswith("+") and not line.startswith("+++")
            for line in patch.unified_diff.splitlines()
        )
        if max_added_lines is not None and added_lines > max_added_lines:
            raise OrchestrationError("candidate diff exceeds max_added_lines")

        metadata = patch.metadata if isinstance(patch.metadata, Mapping) else {}
        path_value = metadata.get("path")
        if not path_value:
            raise OrchestrationError("candidate patch has no materialized path")
        root = candidate_root.resolve()
        try:
            candidate_path = Path(str(path_value)).resolve(strict=True)
            candidate_path.relative_to(root)
        except (OSError, ValueError) as exc:
            raise OrchestrationError(
                "candidate path is outside the controlled candidate root"
            ) from exc
        if not candidate_path.is_dir() or candidate_path.is_symlink():
            raise OrchestrationError("candidate path is not a regular directory")
        snapshot = adapter.snapshot(str(candidate_path), subject_params)
        if not parent.files or not snapshot.files:
            raise OrchestrationError(
                "candidate verification requires complete parent and candidate file snapshots"
            )
        changed_files = {
            path
            for path in set(parent.files).union(snapshot.files)
            if parent.files.get(path) != snapshot.files.get(path)
        }
        if changed_files != touched:
            raise OrchestrationError(
                "candidate materialized files do not match its declared diff"
            )
        if not isinstance(parent.content, str) or not isinstance(snapshot.content, str):
            raise OrchestrationError(
                "candidate adapter does not provide verifiable text content"
            )
        if touched != {entrypoint}:
            raise OrchestrationError(
                "text Subject candidate diff must touch only its entrypoint"
            )
        expected_diff = "\n".join(
            difflib.unified_diff(
                parent.content.splitlines(),
                snapshot.content.splitlines(),
                fromfile="a/" + entrypoint,
                tofile="b/" + entrypoint,
                lineterm="",
            )
        ) + "\n"
        if patch.unified_diff != expected_diff:
            raise OrchestrationError(
                "candidate diff does not match frozen parent and candidate content"
            )
        leaked = [
            literal
            for literal in forbidden_literals
            if len(literal) >= 8 and literal in snapshot.content
        ]
        if leaked:
            raise OrchestrationError("candidate contains a test-only literal")
        expected_hash = metadata.get("subject_hash")
        if expected_hash is not None and snapshot.content_hash != expected_hash:
            raise OrchestrationError("candidate materialized content hash mismatch")
        if snapshot.content_hash == parent.content_hash:
            raise OrchestrationError("candidate does not change the parent Subject")
        candidate_id = str(metadata.get("snapshot_id") or snapshot.content_hash[:16])
        usage = metadata.get("usage", {})
        if not isinstance(usage, Mapping):
            raise OrchestrationError("candidate usage must be an object")
        return candidate_id, candidate_path, snapshot, snapshot.content_hash, dict(usage)

    @staticmethod
    def _frozen_trial_snapshot(
        trial: CandidateTrial,
        frozen_candidates: Mapping[str, SubjectSnapshot],
        adapter: Any,
        subject_params: Mapping[str, Any],
    ) -> SubjectSnapshot:
        snapshot = frozen_candidates.get(trial.candidate_hash)
        if snapshot is None:
            raise OrchestrationError("candidate frozen snapshot is unavailable")
        current = adapter.snapshot(str(trial.candidate_path), subject_params)
        if current.content_hash != trial.candidate_hash:
            raise OrchestrationError("candidate changed between optimization gates")
        return snapshot

    @staticmethod
    def _materialize_selected_candidate(
        adapter: Any,
        snapshot: SubjectSnapshot,
        destination: Path,
        subject_params: Mapping[str, Any],
    ) -> Path:
        materialized = adapter.materialize(snapshot, destination, subject_params)
        verified = adapter.snapshot(str(materialized), subject_params)
        if (
            verified.kind != snapshot.kind
            or verified.content_hash != snapshot.content_hash
        ):
            raise OrchestrationError(
                "selected candidate delivery differs from its frozen snapshot"
            )
        for directory in sorted(
            (item for item in materialized.rglob("*") if item.is_dir()),
            reverse=True,
        ):
            directory.chmod(0o555)
        materialized.chmod(0o555)
        return materialized


__all__ = [
    "CandidateTrial",
    "ComparisonResult",
    "EvalOrchestrator",
    "EvalRun",
    "OptimizationResult",
    "OrchestrationError",
    "ScenarioEvaluation",
    "ValidationAttempt",
]
