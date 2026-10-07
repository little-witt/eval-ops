"""Risk-weighted, bounded test planning for a :mod:`skill_analysis` graph.

This module deliberately stops at auditable case *drafts*.  It maps user seed
cases, expands source-grounded obligations, routes them through a declared
runtime profile, and selects supplementary cases with deterministic greedy
set-cover.  It never claims that a model-proposed semantic oracle is ready for
a frozen hard gate.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import RuntimeCapabilities, RuntimeProfile
from .skill_analysis import Capability, CapabilityGraph, ToolReference


TEST_PLAN_API_VERSION = "aceval.test-plan/v1"
ORACLE_LEVELS = frozenset(
    (
        "deterministic",
        "seed_derived",
        "reference_differential",
        "human_confirmed",
        "model_proposed",
        "unobservable",
    )
)
ORACLE_READY_LEVELS = frozenset(
    ("deterministic", "seed_derived", "reference_differential", "human_confirmed")
)
GAP_TYPES = frozenset(
    ("unsupported", "unobservable", "unconfigured", "unsafe", "unknown")
)
PLAN_OUTCOMES = frozenset(
    ("ready_for_calibration", "needs_user_input", "unsupported_runtime")
)
REQUIREMENT_BINDINGS = frozenset(("contract", "diagnostic"))
PRIORITIES = frozenset(("critical", "high", "medium", "low"))
SPLITS = frozenset(("dev", "validation", "holdout"))
_CONTRACT_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")


class TestPlanningError(ValueError):
    """A graph or seed case cannot be converted into a trustworthy plan."""


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TestPlanningError("%s must be a non-empty string" % label)
    return value.strip()


def _required_id(value: Any, label: str) -> str:
    result = _required_string(value, label)
    if not _CONTRACT_ID.match(result):
        raise TestPlanningError(
            "%s must match %s" % (label, _CONTRACT_ID.pattern)
        )
    return result


def _string_tuple(value: Iterable[Any], label: str) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TestPlanningError("%s must be a sequence of strings" % label)
    result = []
    for item in value:
        result.append(_required_string(item, "%s item" % label))
    return tuple(result)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TestPlanningError("%s must be a JSON object" % label)
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TestPlanningError("%s must be a JSON array" % label)
    return value


def _strict_keys(
    value: Mapping[str, Any], required: Iterable[str], optional: Iterable[str], label: str
) -> None:
    required_set = frozenset(required)
    allowed = required_set | frozenset(optional)
    missing = required_set.difference(value)
    unknown = set(value).difference(allowed)
    if missing:
        raise TestPlanningError(
            "%s is missing fields: %s" % (label, ", ".join(sorted(missing)))
        )
    if unknown:
        raise TestPlanningError(
            "%s has unsupported fields: %s" % (label, ", ".join(sorted(unknown)))
        )


def _finite_positive(value: Any, label: str, allow_zero: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (float(value) < 0 if allow_zero else float(value) <= 0)
    ):
        qualifier = "non-negative" if allow_zero else "positive"
        raise TestPlanningError("%s must be a %s finite number" % (label, qualifier))
    return float(value)


def _dedupe(values: Iterable[str]) -> Tuple[str, ...]:
    result = []
    seen = set()
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return tuple(result)


def _ensure_unique(values: Iterable[str], label: str) -> None:
    seen = set()
    duplicates = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise TestPlanningError(
            "duplicate %s: %s" % (label, ", ".join(sorted(duplicates)))
        )


@dataclass(frozen=True)
class RiskFactors:
    severity: int
    likelihood: int
    side_effect: float = 1.0
    goal_relevance: float = 1.0

    def __post_init__(self) -> None:
        for name in ("severity", "likelihood"):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= 5
            ):
                raise TestPlanningError("risk %s must be an integer from 1 to 5" % name)
        object.__setattr__(
            self,
            "side_effect",
            _finite_positive(self.side_effect, "risk side_effect"),
        )
        object.__setattr__(
            self,
            "goal_relevance",
            _finite_positive(self.goal_relevance, "risk goal_relevance"),
        )

    @property
    def weight(self) -> float:
        return float(
            self.severity * self.likelihood * self.side_effect * self.goal_relevance
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "likelihood": self.likelihood,
            "side_effect": self.side_effect,
            "goal_relevance": self.goal_relevance,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RiskFactors":
        data = _mapping(value, "risk_factors")
        _strict_keys(
            data,
            ("severity", "likelihood", "side_effect", "goal_relevance"),
            (),
            "risk_factors",
        )
        return cls(**dict(data))


@dataclass(frozen=True)
class TestRequirement:
    id: str
    capability_id: str
    dimension: str
    binding: str
    priority: str
    preconditions: Tuple[str, ...]
    stimulus: str
    expected_observables: Tuple[str, ...]
    oracle_strategy: str
    required_runtime_capabilities: Tuple[str, ...]
    source_refs: Tuple[str, ...]
    risk_factors: RiskFactors
    risk_weight: float

    def __post_init__(self) -> None:
        for name in ("id", "capability_id"):
            object.__setattr__(
                self, name, _required_id(getattr(self, name), "requirement.%s" % name)
            )
        for name in ("dimension", "stimulus", "oracle_strategy"):
            object.__setattr__(
                self, name, _required_string(getattr(self, name), "requirement.%s" % name)
            )
        binding = _required_string(self.binding, "requirement.binding")
        priority = _required_string(self.priority, "requirement.priority")
        if binding not in REQUIREMENT_BINDINGS:
            raise TestPlanningError("unsupported requirement binding: %s" % binding)
        if priority not in PRIORITIES:
            raise TestPlanningError("unsupported requirement priority: %s" % priority)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "priority", priority)
        for name in (
            "preconditions",
            "expected_observables",
            "required_runtime_capabilities",
            "source_refs",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), name))
        if not isinstance(self.risk_factors, RiskFactors):
            raise TestPlanningError("requirement.risk_factors must be RiskFactors")
        risk_weight = _finite_positive(self.risk_weight, "requirement.risk_weight")
        if not math.isclose(risk_weight, self.risk_factors.weight, rel_tol=1e-12):
            raise TestPlanningError("requirement.risk_weight does not match its factors")
        object.__setattr__(self, "risk_weight", risk_weight)

    @property
    def critical(self) -> bool:
        return self.priority == "critical"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "capability_id": self.capability_id,
            "dimension": self.dimension,
            "binding": self.binding,
            "priority": self.priority,
            "preconditions": list(self.preconditions),
            "stimulus": self.stimulus,
            "expected_observables": list(self.expected_observables),
            "oracle_strategy": self.oracle_strategy,
            "required_runtime_capabilities": list(
                self.required_runtime_capabilities
            ),
            "source_refs": list(self.source_refs),
            "risk_factors": self.risk_factors.to_dict(),
            "risk_weight": self.risk_weight,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "TestRequirement":
        data = _mapping(value, "requirement")
        fields = (
            "id",
            "capability_id",
            "dimension",
            "binding",
            "priority",
            "preconditions",
            "stimulus",
            "expected_observables",
            "oracle_strategy",
            "required_runtime_capabilities",
            "source_refs",
            "risk_factors",
            "risk_weight",
        )
        _strict_keys(data, fields, (), "requirement")
        return cls(
            id=data["id"],
            capability_id=data["capability_id"],
            dimension=data["dimension"],
            binding=data["binding"],
            priority=data["priority"],
            preconditions=_sequence(
                data["preconditions"], "requirement.preconditions"
            ),
            stimulus=data["stimulus"],
            expected_observables=_sequence(
                data["expected_observables"],
                "requirement.expected_observables",
            ),
            oracle_strategy=data["oracle_strategy"],
            required_runtime_capabilities=_sequence(
                data["required_runtime_capabilities"],
                "requirement.required_runtime_capabilities",
            ),
            source_refs=_sequence(data["source_refs"], "requirement.source_refs"),
            risk_factors=RiskFactors.from_dict(data["risk_factors"]),
            risk_weight=data["risk_weight"],
        )


@dataclass(frozen=True)
class RuntimeGap:
    id: str
    requirement_id: str
    capability: str
    gap_type: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("id", "requirement_id"):
            object.__setattr__(
                self, name, _required_id(getattr(self, name), "runtime_gap.%s" % name)
            )
        for name in ("capability", "reason"):
            object.__setattr__(
                self, name, _required_string(getattr(self, name), "runtime_gap.%s" % name)
            )
        gap_type = _required_string(self.gap_type, "runtime_gap.gap_type")
        if gap_type not in GAP_TYPES:
            raise TestPlanningError("unsupported runtime gap type: %s" % gap_type)
        object.__setattr__(self, "gap_type", gap_type)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "requirement_id": self.requirement_id,
            "capability": self.capability,
            "gap_type": self.gap_type,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RuntimeGap":
        data = _mapping(value, "runtime_gap")
        _strict_keys(
            data, ("id", "requirement_id", "capability", "gap_type", "reason"), (), "runtime_gap"
        )
        return cls(**dict(data))


@dataclass(frozen=True)
class CaseDraft:
    id: str
    split: str
    prompt: str
    requirement_ids: Tuple[str, ...]
    origin: str
    family: str
    oracle_level: str
    oracle_ready: bool
    expected_observables: Tuple[str, ...]
    required_runtime_capabilities: Tuple[str, ...]
    executable: bool
    estimated_cost: float
    needs_user_input: bool
    selection_reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_id(self.id, "case.id"))
        for name in ("prompt", "origin", "family", "selection_reason"):
            object.__setattr__(
                self, name, _required_string(getattr(self, name), "case.%s" % name)
            )
        split = _required_string(self.split, "case.split")
        if split not in SPLITS:
            raise TestPlanningError("unsupported case split: %s" % split)
        object.__setattr__(self, "split", split)
        oracle_level = _required_string(self.oracle_level, "case.oracle_level")
        if oracle_level not in ORACLE_LEVELS:
            raise TestPlanningError("unsupported oracle level: %s" % oracle_level)
        object.__setattr__(self, "oracle_level", oracle_level)
        for name in (
            "requirement_ids",
            "expected_observables",
            "required_runtime_capabilities",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), name))
        for name in ("oracle_ready", "executable", "needs_user_input"):
            if not isinstance(getattr(self, name), bool):
                raise TestPlanningError("case.%s must be a boolean" % name)
        if self.oracle_ready and self.oracle_level not in ORACLE_READY_LEVELS:
            raise TestPlanningError(
                "case.oracle_ready requires a trusted oracle_level"
            )
        object.__setattr__(
            self,
            "estimated_cost",
            _finite_positive(self.estimated_cost, "case.estimated_cost"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "split": self.split,
            "prompt": self.prompt,
            "requirement_ids": list(self.requirement_ids),
            "origin": self.origin,
            "family": self.family,
            "oracle_level": self.oracle_level,
            "oracle_ready": self.oracle_ready,
            "expected_observables": list(self.expected_observables),
            "required_runtime_capabilities": list(
                self.required_runtime_capabilities
            ),
            "executable": self.executable,
            "estimated_cost": self.estimated_cost,
            "needs_user_input": self.needs_user_input,
            "selection_reason": self.selection_reason,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CaseDraft":
        data = _mapping(value, "case")
        fields = (
            "id",
            "split",
            "prompt",
            "requirement_ids",
            "origin",
            "family",
            "oracle_level",
            "oracle_ready",
            "expected_observables",
            "required_runtime_capabilities",
            "executable",
            "estimated_cost",
            "needs_user_input",
            "selection_reason",
        )
        _strict_keys(data, fields, (), "case")
        return cls(
            id=data["id"],
            split=data["split"],
            prompt=data["prompt"],
            requirement_ids=_sequence(data["requirement_ids"], "case.requirement_ids"),
            origin=data["origin"],
            family=data["family"],
            oracle_level=data["oracle_level"],
            oracle_ready=data["oracle_ready"],
            expected_observables=_sequence(
                data["expected_observables"], "case.expected_observables"
            ),
            required_runtime_capabilities=_sequence(
                data["required_runtime_capabilities"],
                "case.required_runtime_capabilities",
            ),
            executable=data["executable"],
            estimated_cost=data["estimated_cost"],
            needs_user_input=data["needs_user_input"],
            selection_reason=data["selection_reason"],
        )


@dataclass(frozen=True)
class PlanningDecision:
    case_id: str
    added_requirement_ids: Tuple[str, ...]
    score: float
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "case_id", _required_id(self.case_id, "decision.case_id")
        )
        object.__setattr__(
            self,
            "added_requirement_ids",
            _string_tuple(self.added_requirement_ids, "decision.added_requirement_ids"),
        )
        object.__setattr__(self, "score", _finite_positive(self.score, "decision.score"))
        object.__setattr__(
            self, "reason", _required_string(self.reason, "decision.reason")
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "added_requirement_ids": list(self.added_requirement_ids),
            "score": self.score,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PlanningDecision":
        data = _mapping(value, "decision")
        _strict_keys(data, ("case_id", "added_requirement_ids", "score", "reason"), (), "decision")
        return cls(
            case_id=data["case_id"],
            added_requirement_ids=_sequence(
                data["added_requirement_ids"], "decision.added_requirement_ids"
            ),
            score=data["score"],
            reason=data["reason"],
        )


@dataclass(frozen=True)
class FreezeBlocker:
    code: str
    message: str
    requirement_id: Optional[str] = None
    capability_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _required_id(self.code, "blocker.code"))
        object.__setattr__(
            self, "message", _required_string(self.message, "blocker.message")
        )
        for name in ("requirement_id", "capability_id"):
            if getattr(self, name) is not None:
                object.__setattr__(
                    self,
                    name,
                    _required_id(getattr(self, name), "blocker.%s" % name),
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "requirement_id": self.requirement_id,
            "capability_id": self.capability_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "FreezeBlocker":
        data = _mapping(value, "blocker")
        _strict_keys(
            data, ("code", "message", "requirement_id", "capability_id"), (), "blocker"
        )
        return cls(**dict(data))


@dataclass(frozen=True)
class TestPlan:
    subject_hash: str
    goal: str
    runtime_profile_state: str
    runtime_capabilities: Tuple[str, ...]
    max_generated_cases: int
    requirements: Tuple[TestRequirement, ...]
    cases: Tuple[CaseDraft, ...]
    runtime_gaps: Tuple[RuntimeGap, ...]
    decisions: Tuple[PlanningDecision, ...]
    uncovered_requirement_ids: Tuple[str, ...]
    budget_excluded_requirement_ids: Tuple[str, ...]
    freeze_blockers: Tuple[FreezeBlocker, ...]
    outcome: str
    api_version: str = TEST_PLAN_API_VERSION

    def __post_init__(self) -> None:
        if self.api_version != TEST_PLAN_API_VERSION:
            raise TestPlanningError(
                "unsupported test plan api_version: %s" % self.api_version
            )
        object.__setattr__(
            self, "subject_hash", _required_string(self.subject_hash, "subject_hash")
        )
        if not self.subject_hash.startswith("sha256:"):
            raise TestPlanningError("subject_hash must be a normalized SHA-256 digest")
        if not re.match(r"^sha256:[0-9a-f]{64}$", self.subject_hash):
            raise TestPlanningError("subject_hash must be a normalized SHA-256 digest")
        if not isinstance(self.goal, str):
            raise TestPlanningError("goal must be a string")
        state = _required_string(self.runtime_profile_state, "runtime_profile_state")
        if state not in ("declared", "unknown"):
            raise TestPlanningError("runtime_profile_state must be declared or unknown")
        object.__setattr__(self, "runtime_profile_state", state)
        object.__setattr__(
            self,
            "runtime_capabilities",
            _string_tuple(self.runtime_capabilities, "runtime_capabilities"),
        )
        if (
            not isinstance(self.max_generated_cases, int)
            or isinstance(self.max_generated_cases, bool)
            or self.max_generated_cases < 0
        ):
            raise TestPlanningError("max_generated_cases must be a non-negative integer")
        for name, expected in (
            ("requirements", TestRequirement),
            ("cases", CaseDraft),
            ("runtime_gaps", RuntimeGap),
            ("decisions", PlanningDecision),
            ("freeze_blockers", FreezeBlocker),
        ):
            values = tuple(getattr(self, name))
            if not all(isinstance(item, expected) for item in values):
                raise TestPlanningError("%s contains an invalid value" % name)
            object.__setattr__(self, name, values)
        for name in (
            "uncovered_requirement_ids",
            "budget_excluded_requirement_ids",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), name))
        outcome = _required_string(self.outcome, "outcome")
        if outcome not in PLAN_OUTCOMES:
            raise TestPlanningError("unsupported plan outcome: %s" % outcome)
        object.__setattr__(self, "outcome", outcome)
        _ensure_unique((item.id for item in self.requirements), "requirement id")
        _ensure_unique((item.id for item in self.cases), "case id")
        _ensure_unique((item.id for item in self.runtime_gaps), "runtime gap id")
        requirement_ids = {item.id for item in self.requirements}
        capability_ids = {item.capability_id for item in self.requirements}
        runtime_values = set(self.runtime_capabilities)
        if self.runtime_profile_state == "unknown" and runtime_values:
            raise TestPlanningError(
                "unknown runtime profile cannot declare runtime capabilities"
            )
        for case in self.cases:
            unknown = set(case.requirement_ids).difference(requirement_ids)
            if unknown:
                raise TestPlanningError(
                    "case %s references unknown requirements: %s"
                    % (case.id, ", ".join(sorted(unknown)))
                )
            expected_executable = (
                self.runtime_profile_state == "declared"
                and set(case.required_runtime_capabilities).issubset(runtime_values)
            )
            if case.executable != expected_executable:
                raise TestPlanningError(
                    "case %s executable flag disagrees with Runtime capabilities"
                    % case.id
                )
        case_ids = {item.id for item in self.cases}
        for decision in self.decisions:
            if decision.case_id not in case_ids:
                raise TestPlanningError("decision references unknown case")
            unknown = set(decision.added_requirement_ids).difference(requirement_ids)
            if unknown:
                raise TestPlanningError("decision references unknown requirements")
        for gap in self.runtime_gaps:
            if gap.requirement_id not in requirement_ids:
                raise TestPlanningError("runtime gap references unknown requirement")
        for blocker in self.freeze_blockers:
            if blocker.requirement_id is not None and blocker.requirement_id not in requirement_ids:
                raise TestPlanningError("blocker references unknown requirement")
            if blocker.capability_id is not None and blocker.capability_id not in capability_ids:
                raise TestPlanningError("blocker references unknown capability")
        for name in ("uncovered_requirement_ids", "budget_excluded_requirement_ids"):
            unknown = set(getattr(self, name)).difference(requirement_ids)
            if unknown:
                raise TestPlanningError("%s references unknown requirements" % name)
        planned_requirement_ids = {
            requirement_id
            for case in self.cases
            for requirement_id in case.requirement_ids
        }
        expected_uncovered = requirement_ids.difference(planned_requirement_ids)
        if set(self.uncovered_requirement_ids) != expected_uncovered:
            raise TestPlanningError(
                "uncovered_requirement_ids disagree with selected cases"
            )
        if not set(self.budget_excluded_requirement_ids).issubset(expected_uncovered):
            raise TestPlanningError(
                "budget_excluded_requirement_ids must be uncovered"
            )
        expected_missing = {
            (requirement.id, capability)
            for requirement in self.requirements
            for capability in requirement.required_runtime_capabilities
            if self.runtime_profile_state == "unknown"
            or capability not in runtime_values
        }
        actual_missing = {
            (gap.requirement_id, gap.capability) for gap in self.runtime_gaps
        }
        if actual_missing != expected_missing:
            raise TestPlanningError(
                "runtime_gaps disagree with Test Requirements and Runtime profile"
            )
        generated_count = sum(case.origin != "seed" for case in self.cases)
        if generated_count > self.max_generated_cases:
            raise TestPlanningError("generated Case budget was exceeded")
        executable_cases = [case for case in self.cases if case.executable]
        if self.cases and not executable_cases and self.runtime_gaps:
            expected_outcome = "unsupported_runtime"
        elif self.freeze_blockers or any(
            case.executable and case.needs_user_input for case in self.cases
        ):
            expected_outcome = "needs_user_input"
        else:
            expected_outcome = "ready_for_calibration"
        if self.outcome != expected_outcome:
            raise TestPlanningError("outcome disagrees with plan evidence")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "api_version": self.api_version,
            "subject_hash": self.subject_hash,
            "goal": self.goal,
            "runtime_profile_state": self.runtime_profile_state,
            "runtime_capabilities": list(self.runtime_capabilities),
            "max_generated_cases": self.max_generated_cases,
            "requirements": [item.to_dict() for item in self.requirements],
            "cases": [item.to_dict() for item in self.cases],
            "runtime_gaps": [item.to_dict() for item in self.runtime_gaps],
            "decisions": [item.to_dict() for item in self.decisions],
            "uncovered_requirement_ids": list(self.uncovered_requirement_ids),
            "budget_excluded_requirement_ids": list(
                self.budget_excluded_requirement_ids
            ),
            "freeze_blockers": [item.to_dict() for item in self.freeze_blockers],
            "outcome": self.outcome,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "TestPlan":
        data = _mapping(value, "test plan")
        fields = (
            "api_version",
            "subject_hash",
            "goal",
            "runtime_profile_state",
            "runtime_capabilities",
            "max_generated_cases",
            "requirements",
            "cases",
            "runtime_gaps",
            "decisions",
            "uncovered_requirement_ids",
            "budget_excluded_requirement_ids",
            "freeze_blockers",
            "outcome",
        )
        _strict_keys(data, fields, (), "test plan")
        return cls(
            api_version=data["api_version"],
            subject_hash=data["subject_hash"],
            goal=data["goal"],
            runtime_profile_state=data["runtime_profile_state"],
            runtime_capabilities=_sequence(
                data["runtime_capabilities"], "test plan runtime_capabilities"
            ),
            max_generated_cases=data["max_generated_cases"],
            requirements=tuple(
                TestRequirement.from_dict(item)
                for item in _sequence(
                    data["requirements"], "test plan requirements"
                )
            ),
            cases=tuple(
                CaseDraft.from_dict(item)
                for item in _sequence(data["cases"], "test plan cases")
            ),
            runtime_gaps=tuple(
                RuntimeGap.from_dict(item)
                for item in _sequence(
                    data["runtime_gaps"], "test plan runtime_gaps"
                )
            ),
            decisions=tuple(
                PlanningDecision.from_dict(item)
                for item in _sequence(data["decisions"], "test plan decisions")
            ),
            uncovered_requirement_ids=_sequence(
                data["uncovered_requirement_ids"],
                "test plan uncovered_requirement_ids",
            ),
            budget_excluded_requirement_ids=_sequence(
                data["budget_excluded_requirement_ids"],
                "test plan budget_excluded_requirement_ids",
            ),
            freeze_blockers=tuple(
                FreezeBlocker.from_dict(item)
                for item in _sequence(
                    data["freeze_blockers"], "test plan freeze_blockers"
                )
            ),
            outcome=data["outcome"],
        )

    @classmethod
    def from_json(cls, value: str) -> "TestPlan":
        try:
            decoded = json.loads(
                value,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_object,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise TestPlanningError("invalid test plan JSON") from exc
        return cls.from_dict(decoded)


def _reject_json_constant(value: str) -> Any:
    raise ValueError("non-standard JSON constant: %s" % value)


def _reject_duplicate_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}  # type: Dict[str, Any]
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


@dataclass(frozen=True)
class PlannerConfig:
    max_generated_cases: int = 12
    generated_split: str = "dev"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_generated_cases, int)
            or isinstance(self.max_generated_cases, bool)
            or self.max_generated_cases < 0
            or self.max_generated_cases > 1000
        ):
            raise TestPlanningError(
                "max_generated_cases must be an integer between 0 and 1000"
            )
        if self.generated_split not in ("dev", "validation"):
            raise TestPlanningError("generated cases may only use dev or validation")


@dataclass(frozen=True)
class _SeedCase:
    id: str
    prompt: str
    split: str
    family: str
    tags: Tuple[str, ...]
    explicit_requirement_ids: Tuple[str, ...]
    expected_observables: Tuple[str, ...]
    oracle_level: str
    oracle_ready: bool
    required_runtime_capabilities: Tuple[str, ...]


@dataclass(frozen=True)
class _Candidate:
    case: CaseDraft
    covers: FrozenSet[str]


_TOKEN = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]+")
_STOP_WORDS = frozenset(
    (
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "for",
        "with",
        "when",
        "if",
        "test",
        "skill",
        "case",
        "should",
        "must",
        "要求",
        "如果",
        "测试",
        "技能",
    )
)


def _tokens(value: str) -> FrozenSet[str]:
    return frozenset(
        token.lower()
        for token in _TOKEN.findall(value)
        if token.lower() not in _STOP_WORDS and len(token) > 1
    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower().strip()).strip("-")
    if normalized:
        return normalized[:80]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return "item-%s" % digest


def _unique_id(base: str, used: set) -> str:
    candidate = base
    index = 2
    while candidate in used:
        candidate = "%s-%d" % (base, index)
        index += 1
    used.add(candidate)
    return candidate


def _priority(weight: float, severity: int) -> str:
    if severity == 5 and weight >= 25:
        return "critical"
    if weight >= 16:
        return "high"
    if weight >= 8:
        return "medium"
    return "low"


def _goal_factor(goal: str, *values: str) -> float:
    goal_tokens = _tokens(goal)
    if goal_tokens and goal_tokens.intersection(_tokens(" ".join(values))):
        return 1.5
    return 1.0


def _risk_factors(
    capability: Capability,
    goal: str,
    severity: int,
    likelihood: int,
    *relevance: str
) -> RiskFactors:
    return RiskFactors(
        severity=severity,
        likelihood=likelihood,
        side_effect=1.5 if capability.side_effects else 1.0,
        goal_relevance=_goal_factor(goal, capability.name, *relevance),
    )


def _tool_map(graph: CapabilityGraph) -> Dict[str, ToolReference]:
    return {item.name: item for item in graph.tools}


def _base_runtime_requirements(
    capability: Capability, tools: Mapping[str, ToolReference]
) -> Tuple[str, ...]:
    values = ["fresh_session"]
    if capability.inputs or capability.preconditions:
        values.append("workspace_fixture")
    if _has_artifact_output(capability):
        values.append("artifact_output")
    for name in capability.tools:
        tool = tools.get(name)
        if tool is not None:
            values.extend(tool.required_runtime_capabilities)
    return tuple(sorted(set(values)))


def _has_artifact_output(capability: Capability) -> bool:
    return any(
        bool(
            re.search(
                r"(?:^|[/\\])[^/\\]+\.(?:json|csv|txt|md|yaml|yml|xml|html|py|js|ts)$",
                output.strip(),
                re.IGNORECASE,
            )
        )
        for output in capability.outputs
    )


def _requirement(
    used_ids: set,
    capability: Capability,
    suffix: str,
    dimension: str,
    binding: str,
    preconditions: Iterable[str],
    stimulus: str,
    expected: Iterable[str],
    oracle_strategy: str,
    runtime_capabilities: Iterable[str],
    source_refs: Iterable[str],
    factors: RiskFactors,
) -> TestRequirement:
    requirement_id = _unique_id(
        "req.%s.%s" % (capability.id[4:] if capability.id.startswith("cap.") else capability.id, _slug(suffix)),
        used_ids,
    )
    return TestRequirement(
        id=requirement_id,
        capability_id=capability.id,
        dimension=dimension,
        binding=binding,
        priority=_priority(factors.weight, factors.severity),
        preconditions=_dedupe(preconditions),
        stimulus=stimulus,
        expected_observables=_dedupe(expected),
        oracle_strategy=oracle_strategy,
        required_runtime_capabilities=tuple(sorted(set(runtime_capabilities))),
        source_refs=_dedupe(source_refs),
        risk_factors=factors,
        risk_weight=factors.weight,
    )


def _requirements(graph: CapabilityGraph, goal: str) -> Tuple[TestRequirement, ...]:
    used_ids = set()
    tools = _tool_map(graph)
    result = []
    for capability in graph.capabilities:
        base_runtime = _base_runtime_requirements(capability, tools)
        binding = "diagnostic" if capability.inferred else "contract"
        source_refs = tuple(
            "%s#source-%d" % (capability.id, index)
            for index, _ in enumerate(capability.source_refs)
        )
        happy_factors = _risk_factors(capability, goal, 3, 4, "happy", "output")
        expected = (
            tuple("产出 Skill 声明的结果：%s" % item for item in capability.outputs)
            or ("完成 Skill 声明的能力：%s" % capability.name,)
        )
        result.append(
            _requirement(
                used_ids,
                capability,
                "happy",
                "happy_path",
                binding,
                capability.preconditions,
                "执行 Skill 声明的能力：%s" % capability.name,
                expected,
                "workspace_state" if _has_artifact_output(capability) else "semantic_rubric",
                base_runtime,
                source_refs,
                happy_factors,
            )
        )
        for branch in capability.branches:
            condition = branch.condition
            negative = bool(
                re.search(
                    r"\b(?:invalid|malformed|empty|missing|fail|error|denied|timeout)\b|"
                    r"无效|错误|失败|缺失|为空|拒绝|超时",
                    condition,
                    re.I,
                )
            )
            factors = _risk_factors(
                capability, goal, 4 if negative else 3, 3, condition
            )
            result.append(
                _requirement(
                    used_ids,
                    capability,
                    "branch-%s" % branch.id,
                    "negative" if negative else "branch",
                    binding,
                    capability.preconditions,
                    condition,
                    ("遵循 Skill 明确声明的分支行为：%s" % condition,),
                    "output_invariant" if capability.outputs else "semantic_rubric",
                    base_runtime,
                    (branch.id,),
                    factors,
                )
            )
        for index, precondition in enumerate(capability.preconditions, 1):
            factors = _risk_factors(capability, goal, 4, 3, precondition, "negative")
            result.append(
                _requirement(
                    used_ids,
                    capability,
                    "precondition-%d" % index,
                    "negative",
                    binding,
                    (),
                    "Remove or violate precondition: %s" % precondition,
                    ("清晰报告失败或安全恢复，且不产生未授权副作用。",),
                    "output_invariant+workspace_diff",
                    set(base_runtime) | {"workspace_fixture"},
                    source_refs,
                    factors,
                )
            )
        for index, risk in enumerate(capability.risks, 1):
            factors = _risk_factors(capability, goal, 5, 4, risk, "risk")
            result.append(
                _requirement(
                    used_ids,
                    capability,
                    "risk-%d" % index,
                    "risk",
                    binding,
                    capability.preconditions,
                    "Exercise the declared risk boundary: %s" % risk,
                    ("不得发生 Skill 声明的禁止或不安全结果：%s" % risk,),
                    "workspace_diff+output_invariant",
                    base_runtime,
                    source_refs,
                    factors,
                )
            )
        external_tools = [
            tools[name]
            for name in capability.tools
            if name in tools and tools[name].kind not in ("workspace",)
        ]
        recovery_declared = any(
            re.search(r"\b(?:fail|error|timeout|retry|recover)\b|失败|错误|超时|重试|恢复", item.condition, re.I)
            for item in capability.branches
        )
        if external_tools or recovery_declared:
            names = ", ".join(item.name for item in external_tools) or "declared tool"
            factors = _risk_factors(capability, goal, 5, 3, names, "recovery")
            result.append(
                _requirement(
                    used_ids,
                    capability,
                    "tool-failure",
                    "recovery",
                    binding,
                    capability.preconditions,
                    "Inject a failure from: %s" % names,
                    ("Skill 必须报告或恢复工具失败，且不留下不安全的部分修改。",),
                    "fault_signal+workspace_diff",
                    set(base_runtime) | {"fault_injection"},
                    source_refs,
                    factors,
                )
            )
        if capability.side_effects:
            factors = _risk_factors(capability, goal, 4, 3, "idempotency", "repeat")
            result.append(
                _requirement(
                    used_ids,
                    capability,
                    "idempotency",
                    "idempotency",
                    binding,
                    capability.preconditions,
                    "Repeat the same request against the resulting state.",
                    ("Repeated execution has the declared or safely bounded side effects.",),
                    "workspace_diff",
                    set(base_runtime) | {"stateful_replay"},
                    source_refs,
                    factors,
                )
            )
        for index, transition in enumerate(capability.state_transitions, 1):
            factors = _risk_factors(capability, goal, 4, 3, transition, "state")
            result.append(
                _requirement(
                    used_ids,
                    capability,
                    "state-%d" % index,
                    "state_transition",
                    binding,
                    capability.preconditions,
                    "Exercise the declared state transition: %s" % transition,
                    ("The observable state matches the declared transition.",),
                    "workspace_state",
                    set(base_runtime) | {"workspace_fixture"},
                    source_refs,
                    factors,
                )
            )
        if len(capability.steps) >= 2 and capability.tools:
            factors = _risk_factors(capability, goal, 2, 3, "step ordering")
            result.append(
                _requirement(
                    used_ids,
                    capability,
                    "step-order",
                    "step_ordering",
                    "diagnostic",
                    capability.preconditions,
                    "Observe whether the declared multi-step workflow is coherent.",
                    ("Trace is consistent with the declared steps; alternate correct plans remain allowed.",),
                    "trace_contract",
                    set(base_runtime) | {"canonical_trace"},
                    source_refs,
                    factors,
                )
            )
    return tuple(sorted(result, key=lambda item: item.id))


def _runtime_values(
    value: Optional[Iterable[str]],
) -> Tuple[Optional[FrozenSet[str]], str]:
    if value is None:
        return None, "unknown"
    if isinstance(value, RuntimeProfile):
        return frozenset(value.capabilities.values), "declared"
    if isinstance(value, RuntimeCapabilities):
        return frozenset(value.values), "declared"
    if isinstance(value, (str, bytes)):
        raise TestPlanningError("runtime_capabilities must be an iterable, not a string")
    normalized = frozenset(
        _required_string(item, "runtime capability") for item in value
    )
    return normalized, "declared"


def _gap_type(capability: str, profile_known: bool) -> str:
    if not profile_known:
        return "unknown"
    if capability in ("canonical_trace", "artifact_output", "workspace_state"):
        return "unobservable"
    if capability in ("authentication", "permission_profile", "secrets"):
        return "unconfigured"
    if capability.startswith("unsafe_"):
        return "unsafe"
    return "unsupported"


def _runtime_gaps(
    requirements: Sequence[TestRequirement], runtime: Optional[FrozenSet[str]]
) -> Tuple[RuntimeGap, ...]:
    gaps = []
    for requirement in requirements:
        missing = (
            set(requirement.required_runtime_capabilities)
            if runtime is None
            else set(requirement.required_runtime_capabilities).difference(runtime)
        )
        for capability in sorted(missing):
            gap_type = _gap_type(capability, runtime is not None)
            gaps.append(
                RuntimeGap(
                    id="gap.%s.%s" % (requirement.id[4:] if requirement.id.startswith("req.") else requirement.id, _slug(capability)),
                    requirement_id=requirement.id,
                    capability=capability,
                    gap_type=gap_type,
                    reason=(
                        "Runtime profile was not supplied; support is unknown."
                        if runtime is None
                        else "Runtime profile does not declare %s." % capability
                    ),
                )
            )
    return tuple(gaps)


def _seed_case(value: Any, index: int) -> _SeedCase:
    data = _mapping(value, "seed case")
    case_id = _required_string(data.get("id"), "seed case id")
    prompt = _required_string(data.get("prompt"), "seed case prompt")
    split = str(data.get("split", "dev"))
    if split not in SPLITS:
        raise TestPlanningError("unsupported seed case split: %s" % split)
    metadata = data.get("metadata", {})
    if metadata is None:
        metadata = {}
    metadata = _mapping(metadata, "seed case metadata")
    family = data.get("family_id", metadata.get("family_id"))
    if family is None:
        family = "seed.%s" % _slug(case_id)
    family = _required_string(family, "seed case family_id")
    explicit = data.get("requirement_ids", metadata.get("requirement_ids", ()))
    tags = data.get("tags", ())
    expected = data.get("expected_observables", ())
    if isinstance(expected, str):
        expected = (expected,)
    has_exact = any(
        key in data
        for key in ("expected_output", "expected_records", "forbidden_records")
    )
    oracle_level = data.get("oracle_level")
    if oracle_level is None:
        if has_exact:
            oracle_level = "seed_derived"
        elif expected:
            oracle_level = "human_confirmed"
        else:
            oracle_level = "unobservable"
    if oracle_level not in ORACLE_LEVELS:
        raise TestPlanningError("unsupported seed oracle_level: %s" % oracle_level)
    runtime = data.get("required_runtime_capabilities", ())
    return _SeedCase(
        id=case_id,
        prompt=prompt,
        split=split,
        family=family,
        tags=_string_tuple(tags, "seed tags"),
        explicit_requirement_ids=_string_tuple(explicit, "seed requirement_ids"),
        expected_observables=_string_tuple(expected, "seed expected_observables"),
        oracle_level=oracle_level,
        oracle_ready=(has_exact and oracle_level in ORACLE_READY_LEVELS),
        required_runtime_capabilities=_string_tuple(runtime, "seed runtime capabilities"),
    )


def _map_seed(
    seed: _SeedCase,
    requirements: Sequence[TestRequirement],
    capabilities: Mapping[str, Capability],
) -> Tuple[str, ...]:
    by_id = {item.id: item for item in requirements}
    unknown = set(seed.explicit_requirement_ids).difference(by_id)
    if unknown:
        raise TestPlanningError(
            "seed case %s references unknown requirements: %s"
            % (seed.id, ", ".join(sorted(unknown)))
        )
    if seed.explicit_requirement_ids:
        return tuple(seed.explicit_requirement_ids)
    tag_set = {item.casefold() for item in seed.tags}
    mapped = []
    for tag in tag_set:
        if tag in by_id:
            mapped.append(tag)
            continue
        for capability in capabilities.values():
            capability_labels = {
                capability.id.casefold(),
                _slug(capability.name).casefold(),
            }
            if tag in capability_labels:
                happy = next(
                    (
                        requirement.id
                        for requirement in requirements
                        if requirement.capability_id == capability.id
                        and requirement.dimension == "happy_path"
                    ),
                    None,
                )
                if happy is not None:
                    mapped.append(happy)
                continue
            composite_prefixes = tuple(
                "%s:" % label for label in capability_labels
            )
            if tag.startswith(composite_prefixes):
                dimension = tag.split(":", 1)[1]
                mapped.extend(
                    requirement.id
                    for requirement in requirements
                    if requirement.capability_id == capability.id
                    and requirement.dimension.casefold() == dimension
                )
        if len(capabilities) == 1:
            mapped.extend(
                requirement.id
                for requirement in requirements
                if requirement.dimension.casefold() == tag
            )
    if mapped:
        return tuple(mapped)
    prompt_tokens = _tokens(seed.prompt)
    matching_capabilities = [
        capability
        for capability in capabilities.values()
        if _tokens(capability.name) and _tokens(capability.name).issubset(prompt_tokens)
    ]
    if not matching_capabilities and len(capabilities) == 1:
        matching_capabilities = list(capabilities.values())
    for capability in matching_capabilities:
        happy = next(
            (
                requirement
                for requirement in requirements
                if requirement.capability_id == capability.id
                and requirement.dimension == "happy_path"
            ),
            None,
        )
        if happy is not None:
            mapped.append(happy.id)
        for requirement in requirements:
            if requirement.capability_id != capability.id or requirement.dimension not in (
                "branch",
                "negative",
                "risk",
            ):
                continue
            distinctive = _tokens(requirement.stimulus)
            if distinctive and len(distinctive.intersection(prompt_tokens)) >= 2:
                mapped.append(requirement.id)
    return _dedupe(mapped)


def _is_executable(
    required: Iterable[str], runtime: Optional[FrozenSet[str]]
) -> bool:
    return runtime is not None and set(required).issubset(runtime)


def _generated_oracle_level(requirement: TestRequirement) -> str:
    # The D20 generator creates a prompt and expected-observable draft, but it
    # does not synthesize an executable semantic assertion or fixture.  A
    # source-grounded workspace strategy is not, by itself, a trusted Oracle.
    return "model_proposed"


def _candidate(
    requirement: TestRequirement,
    requirements: Sequence[TestRequirement],
    runtime: Optional[FrozenSet[str]],
    split: str,
) -> _Candidate:
    covered = {requirement.id}
    covered_requirements = [
        item for item in requirements if item.id in covered
    ]
    runtime_required = tuple(
        sorted(
            {
                capability
                for item in covered_requirements
                for capability in item.required_runtime_capabilities
            }
        )
    )
    oracle_level = _generated_oracle_level(requirement)
    expected = tuple(
        observable
        for item in covered_requirements
        for observable in item.expected_observables
    )
    estimated_cost = 1.0 + 0.15 * len(runtime_required)
    if requirement.dimension in ("recovery", "idempotency", "state_transition"):
        estimated_cost += 1.0
    case = CaseDraft(
        id="generated.%s" % (requirement.id[4:] if requirement.id.startswith("req.") else requirement.id),
        split=split,
        prompt=(
            "请针对 Skill 中的能力“%s”执行真实任务：%s。"
            "重点验证：%s。执行时必须先读取并遵循 Skill，"
            "最后说明实际采取的步骤、产出和未满足项；不要把能力名称本身当作结果。"
            % (
                requirement.capability_id,
                requirement.stimulus,
                "；".join(expected),
            )
        ),
        requirement_ids=tuple(sorted(covered)),
        origin="requirement_synthesis",
        family="%s.%s" % (requirement.capability_id, requirement.dimension),
        oracle_level=oracle_level,
        oracle_ready=oracle_level in ORACLE_READY_LEVELS,
        expected_observables=expected,
        required_runtime_capabilities=runtime_required,
        executable=_is_executable(runtime_required, runtime),
        estimated_cost=estimated_cost,
        needs_user_input=oracle_level not in ORACLE_READY_LEVELS,
        selection_reason=(
            "围绕未覆盖要求 %s 生成；对应能力 %s，评测维度为 %s，"
            "依据 Skill 来源 %s，检查 %s。"
            % (
                requirement.id,
                requirement.capability_id,
                requirement.dimension,
                "、".join(requirement.source_refs) or "未声明来源",
                "；".join(expected),
            )
        ),
    )
    return _Candidate(case=case, covers=frozenset(covered))


class TestPlanner:
    """Create deterministic Test Requirements and a bounded case selection."""

    id = "risk-weighted-test-planner/v1"

    def __init__(self, config: Optional[PlannerConfig] = None) -> None:
        self.config = config or PlannerConfig()

    def plan(
        self,
        graph: CapabilityGraph,
        *,
        seed_cases: Sequence[Mapping[str, Any]] = (),
        goal: str = "",
        runtime_capabilities: Optional[Iterable[str]] = None,
    ) -> TestPlan:
        if not isinstance(graph, CapabilityGraph):
            raise TestPlanningError("graph must be a CapabilityGraph")
        if not isinstance(goal, str):
            raise TestPlanningError("goal must be a string")
        runtime, runtime_state = _runtime_values(runtime_capabilities)
        requirements = _requirements(graph, goal)
        requirement_by_id = {item.id: item for item in requirements}
        capabilities = {item.id: item for item in graph.capabilities}
        normalized_seeds = tuple(
            _seed_case(value, index) for index, value in enumerate(seed_cases, 1)
        )
        _ensure_unique((item.id for item in normalized_seeds), "seed case id")
        cases = []  # type: List[CaseDraft]
        covered = set()
        unmapped_seed_ids = []
        for seed in normalized_seeds:
            mapped = _map_seed(seed, requirements, capabilities)
            if not mapped:
                unmapped_seed_ids.append(seed.id)
            required = set(seed.required_runtime_capabilities)
            for requirement_id in mapped:
                required.update(
                    requirement_by_id[requirement_id].required_runtime_capabilities
                )
            oracle_ready = seed.oracle_ready
            cases.append(
                CaseDraft(
                    id=seed.id,
                    split=seed.split,
                    prompt=seed.prompt,
                    requirement_ids=mapped,
                    origin="seed",
                    family=seed.family,
                    oracle_level=seed.oracle_level,
                    oracle_ready=oracle_ready,
                    expected_observables=seed.expected_observables,
                    required_runtime_capabilities=tuple(sorted(required)),
                    executable=_is_executable(required, runtime),
                    estimated_cost=1.0 + 0.15 * len(required),
                    needs_user_input=not oracle_ready,
                    selection_reason=(
                        "Mapped from explicit requirement_ids or conservative seed semantics."
                        if mapped
                        else "Seed case could not be mapped to a declared requirement."
                    ),
                )
            )
            covered.update(mapped)

        candidates = [
            _candidate(
                requirement,
                requirements,
                runtime,
                self.config.generated_split,
            )
            for requirement in requirements
            if requirement.id not in covered
        ]
        candidates = [item for item in candidates if item.case.executable]
        decisions = []
        selected_ids = {item.id for item in cases}
        for _ in range(self.config.max_generated_cases):
            ranked = []
            for candidate in candidates:
                if candidate.case.id in selected_ids:
                    continue
                new_ids = candidate.covers.difference(covered)
                if not new_ids:
                    continue
                new_weight = sum(requirement_by_id[item].risk_weight for item in new_ids)
                score = new_weight / candidate.case.estimated_cost
                ranked.append((score, candidate.case.id, candidate, tuple(sorted(new_ids))))
            if not ranked:
                break
            ranked.sort(key=lambda item: (-item[0], item[1]))
            score, _, selected, new_ids = ranked[0]
            cases.append(selected.case)
            selected_ids.add(selected.case.id)
            covered.update(selected.covers)
            decisions.append(
                PlanningDecision(
                    case_id=selected.case.id,
                    added_requirement_ids=new_ids,
                    score=score,
                    reason=(
                        "Selected by greedy uncovered-risk/estimated-cost score %.6f."
                        % score
                    ),
                )
            )

        uncovered = tuple(
            item.id for item in requirements if item.id not in covered
        )
        feasible_candidate_requirements = {
            requirement_id
            for candidate in candidates
            for requirement_id in candidate.covers
        }
        budget_excluded = tuple(
            item
            for item in uncovered
            if item in feasible_candidate_requirements
        )
        gaps = _runtime_gaps(requirements, runtime)
        blockers = []
        for seed_id in unmapped_seed_ids:
            blockers.append(
                FreezeBlocker(
                    code="seed_case_unmapped",
                    message="Seed case %s is not traceable to a Test Requirement." % seed_id,
                )
            )
        for ambiguity in graph.ambiguities:
            if ambiguity.reason_code in (
                "missing_declared_output",
                "explicit_placeholder",
                "subjective_or_vague",
                "unassigned_tool_dependency",
            ):
                blockers.append(
                    FreezeBlocker(
                        code="ambiguous_contract",
                        message=ambiguity.message,
                        capability_id=ambiguity.capability_id,
                    )
                )
        cases_by_requirement = {
            requirement.id: [
                case for case in cases if requirement.id in case.requirement_ids
            ]
            for requirement in requirements
        }
        for capability in graph.capabilities:
            if not any(
                case.requirement_ids
                and any(
                    requirement_by_id[requirement_id].capability_id == capability.id
                    for requirement_id in case.requirement_ids
                )
                for case in cases
            ):
                blockers.append(
                    FreezeBlocker(
                        code="capability_uncovered",
                        message="Declared capability has no selected Case.",
                        capability_id=capability.id,
                    )
                )
        for requirement in requirements:
            if not requirement.critical:
                continue
            linked = cases_by_requirement[requirement.id]
            if not linked:
                code = "critical_requirement_uncovered"
                message = "Critical requirement has no selected case."
            elif not any(case.executable for case in linked):
                code = "critical_requirement_not_executable"
                message = "Critical requirement has no Runtime-executable case."
            elif not any(case.executable and case.oracle_ready for case in linked):
                code = "critical_requirement_oracle_not_ready"
                message = "Critical requirement has no executable trusted Oracle."
            else:
                continue
            blockers.append(
                FreezeBlocker(
                    code=code,
                    message=message,
                    requirement_id=requirement.id,
                    capability_id=requirement.capability_id,
                )
            )
        executable_cases = [case for case in cases if case.executable]
        if cases and not executable_cases and gaps:
            outcome = "unsupported_runtime"
        elif blockers or any(
            case.executable and case.needs_user_input for case in cases
        ):
            outcome = "needs_user_input"
        else:
            outcome = "ready_for_calibration"
        return TestPlan(
            subject_hash=graph.subject_hash,
            goal=goal,
            runtime_profile_state=runtime_state,
            runtime_capabilities=tuple(sorted(runtime or ())),
            max_generated_cases=self.config.max_generated_cases,
            requirements=requirements,
            cases=tuple(cases),
            runtime_gaps=gaps,
            decisions=tuple(decisions),
            uncovered_requirement_ids=uncovered,
            budget_excluded_requirement_ids=budget_excluded,
            freeze_blockers=tuple(blockers),
            outcome=outcome,
        )


def plan_tests(
    graph: CapabilityGraph,
    *,
    seed_cases: Sequence[Mapping[str, Any]] = (),
    goal: str = "",
    runtime_capabilities: Optional[Iterable[str]] = None,
    max_generated_cases: int = 12,
) -> TestPlan:
    """Functional facade for the default planner."""

    return TestPlanner(PlannerConfig(max_generated_cases=max_generated_cases)).plan(
        graph,
        seed_cases=seed_cases,
        goal=goal,
        runtime_capabilities=runtime_capabilities,
    )


__all__ = [
    "GAP_TYPES",
    "ORACLE_LEVELS",
    "ORACLE_READY_LEVELS",
    "PLAN_OUTCOMES",
    "TEST_PLAN_API_VERSION",
    "CaseDraft",
    "FreezeBlocker",
    "PlannerConfig",
    "PlanningDecision",
    "RiskFactors",
    "RuntimeGap",
    "TestPlan",
    "TestPlanner",
    "TestPlanningError",
    "TestRequirement",
    "plan_tests",
]
