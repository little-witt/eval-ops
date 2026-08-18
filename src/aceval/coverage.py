"""Traceable multi-stage coverage for complex Skill test plans.

Coverage is intentionally reported as four separate claims.  A planned case
is not automatically executable, an executable case is not automatically
oracle-ready, and none of those static states prove that a path was observed
in a real run.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .test_planning import CaseDraft, TestPlan


COVERAGE_REPORT_API_VERSION = "aceval.coverage-report/v1"
COVERAGE_STAGES = ("planned", "executable", "oracle_ready", "observed")


class CoverageError(ValueError):
    """Coverage evidence is inconsistent with the frozen Test Plan."""


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoverageError("%s must be a non-empty string" % label)
    return value.strip()


def _string_tuple(value: Iterable[Any], label: str) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise CoverageError("%s must be a sequence of strings" % label)
    return tuple(_required_string(item, "%s item" % label) for item in value)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CoverageError("%s must be a JSON object" % label)
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CoverageError("%s must be a JSON array" % label)
    return value


def _strict_keys(
    value: Mapping[str, Any], required: Iterable[str], optional: Iterable[str], label: str
) -> None:
    required_set = frozenset(required)
    allowed = required_set | frozenset(optional)
    missing = required_set.difference(value)
    unknown = set(value).difference(allowed)
    if missing:
        raise CoverageError(
            "%s is missing fields: %s" % (label, ", ".join(sorted(missing)))
        )
    if unknown:
        raise CoverageError(
            "%s has unsupported fields: %s" % (label, ", ".join(sorted(unknown)))
        )


def _count(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CoverageError("%s must be a non-negative integer" % label)
    return value


def _weight(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise CoverageError("%s must be a non-negative finite number" % label)
    return float(value)


def _ratio(numerator: float, denominator: float) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def _ensure_unique(values: Iterable[str], label: str) -> None:
    seen = set()
    duplicates = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise CoverageError(
            "duplicate %s: %s" % (label, ", ".join(sorted(duplicates)))
        )


@dataclass(frozen=True)
class CoverageMetric:
    covered: int
    total: int
    covered_weight: float
    total_weight: float

    def __post_init__(self) -> None:
        covered = _count(self.covered, "coverage covered")
        total = _count(self.total, "coverage total")
        covered_weight = _weight(self.covered_weight, "coverage covered_weight")
        total_weight = _weight(self.total_weight, "coverage total_weight")
        if covered > total:
            raise CoverageError("coverage covered cannot exceed total")
        if covered_weight > total_weight and not math.isclose(
            covered_weight, total_weight, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise CoverageError("coverage covered_weight cannot exceed total_weight")
        object.__setattr__(self, "covered", covered)
        object.__setattr__(self, "total", total)
        object.__setattr__(self, "covered_weight", covered_weight)
        object.__setattr__(self, "total_weight", total_weight)

    @property
    def ratio(self) -> float:
        return _ratio(float(self.covered), float(self.total))

    @property
    def weighted_ratio(self) -> float:
        return _ratio(self.covered_weight, self.total_weight)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "covered": self.covered,
            "total": self.total,
            "ratio": self.ratio,
            "covered_weight": self.covered_weight,
            "total_weight": self.total_weight,
            "weighted_ratio": self.weighted_ratio,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CoverageMetric":
        data = _mapping(value, "coverage metric")
        _strict_keys(
            data,
            (
                "covered",
                "total",
                "ratio",
                "covered_weight",
                "total_weight",
                "weighted_ratio",
            ),
            (),
            "coverage metric",
        )
        metric = cls(
            covered=data["covered"],
            total=data["total"],
            covered_weight=data["covered_weight"],
            total_weight=data["total_weight"],
        )
        for supplied, computed, label in (
            (data["ratio"], metric.ratio, "ratio"),
            (data["weighted_ratio"], metric.weighted_ratio, "weighted_ratio"),
        ):
            supplied_value = _weight(supplied, "coverage %s" % label)
            if not math.isclose(supplied_value, computed, rel_tol=1e-12, abs_tol=1e-12):
                raise CoverageError("coverage %s does not match counts" % label)
        return metric


@dataclass(frozen=True)
class RequirementCoverage:
    requirement_id: str
    capability_id: str
    priority: str
    risk_weight: float
    case_ids: Tuple[str, ...]
    executable_case_ids: Tuple[str, ...]
    oracle_ready_case_ids: Tuple[str, ...]
    observed_case_ids: Tuple[str, ...]
    direct_observation: bool
    planned: bool
    executable: bool
    oracle_ready: bool
    observed: bool
    gap_reasons: Tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("requirement_id", "capability_id", "priority"):
            object.__setattr__(
                self, name, _required_string(getattr(self, name), name)
            )
        object.__setattr__(self, "risk_weight", _weight(self.risk_weight, "risk_weight"))
        for name in (
            "case_ids",
            "executable_case_ids",
            "oracle_ready_case_ids",
            "observed_case_ids",
            "gap_reasons",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), name))
        for name in COVERAGE_STAGES:
            if not isinstance(getattr(self, name), bool):
                raise CoverageError("%s must be a boolean" % name)
        if not isinstance(self.direct_observation, bool):
            raise CoverageError("direct_observation must be a boolean")
        case_ids = set(self.case_ids)
        if not set(self.executable_case_ids).issubset(case_ids):
            raise CoverageError("executable cases must be planned cases")
        if not set(self.oracle_ready_case_ids).issubset(self.executable_case_ids):
            raise CoverageError("oracle-ready cases must be executable cases")
        if not set(self.observed_case_ids).issubset(self.executable_case_ids):
            raise CoverageError("observed cases must be executable cases")
        if self.direct_observation and not self.executable_case_ids:
            raise CoverageError(
                "direct observation requires an executable planned case"
            )
        expected_flags = (
            bool(self.case_ids),
            bool(self.executable_case_ids),
            bool(self.oracle_ready_case_ids),
            bool(self.observed_case_ids) or self.direct_observation,
        )
        supplied_flags = tuple(getattr(self, item) for item in COVERAGE_STAGES)
        if expected_flags != supplied_flags:
            raise CoverageError("coverage flags disagree with their case evidence")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "capability_id": self.capability_id,
            "priority": self.priority,
            "risk_weight": self.risk_weight,
            "case_ids": list(self.case_ids),
            "executable_case_ids": list(self.executable_case_ids),
            "oracle_ready_case_ids": list(self.oracle_ready_case_ids),
            "observed_case_ids": list(self.observed_case_ids),
            "direct_observation": self.direct_observation,
            "planned": self.planned,
            "executable": self.executable,
            "oracle_ready": self.oracle_ready,
            "observed": self.observed,
            "gap_reasons": list(self.gap_reasons),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RequirementCoverage":
        data = _mapping(value, "requirement coverage")
        fields = (
            "requirement_id",
            "capability_id",
            "priority",
            "risk_weight",
            "case_ids",
            "executable_case_ids",
            "oracle_ready_case_ids",
            "observed_case_ids",
            "direct_observation",
            "planned",
            "executable",
            "oracle_ready",
            "observed",
            "gap_reasons",
        )
        _strict_keys(data, fields, (), "requirement coverage")
        return cls(
            requirement_id=data["requirement_id"],
            capability_id=data["capability_id"],
            priority=data["priority"],
            risk_weight=data["risk_weight"],
            case_ids=_sequence(data["case_ids"], "coverage case_ids"),
            executable_case_ids=_sequence(
                data["executable_case_ids"], "coverage executable_case_ids"
            ),
            oracle_ready_case_ids=_sequence(
                data["oracle_ready_case_ids"], "coverage oracle_ready_case_ids"
            ),
            observed_case_ids=_sequence(
                data["observed_case_ids"], "coverage observed_case_ids"
            ),
            direct_observation=data["direct_observation"],
            planned=data["planned"],
            executable=data["executable"],
            oracle_ready=data["oracle_ready"],
            observed=data["observed"],
            gap_reasons=_sequence(data["gap_reasons"], "coverage gap_reasons"),
        )


@dataclass(frozen=True)
class CoverageReport:
    subject_hash: str
    test_plan_api_version: str
    observed_case_ids: Tuple[str, ...]
    requirements: Tuple[RequirementCoverage, ...]
    planned: CoverageMetric
    executable: CoverageMetric
    oracle_ready: CoverageMetric
    observed: CoverageMetric
    api_version: str = COVERAGE_REPORT_API_VERSION

    def __post_init__(self) -> None:
        if self.api_version != COVERAGE_REPORT_API_VERSION:
            raise CoverageError(
                "unsupported coverage api_version: %s" % self.api_version
            )
        object.__setattr__(
            self, "subject_hash", _required_string(self.subject_hash, "subject_hash")
        )
        object.__setattr__(
            self,
            "test_plan_api_version",
            _required_string(self.test_plan_api_version, "test_plan_api_version"),
        )
        object.__setattr__(
            self,
            "observed_case_ids",
            _string_tuple(self.observed_case_ids, "observed_case_ids"),
        )
        requirements = tuple(self.requirements)
        if not all(isinstance(item, RequirementCoverage) for item in requirements):
            raise CoverageError("requirements contain an invalid value")
        object.__setattr__(self, "requirements", requirements)
        _ensure_unique(
            (item.requirement_id for item in requirements), "requirement coverage id"
        )
        row_observed_cases = {
            case_id for item in requirements for case_id in item.observed_case_ids
        }
        if row_observed_cases != set(self.observed_case_ids):
            raise CoverageError(
                "observed_case_ids disagree with requirement evidence"
            )
        for stage in COVERAGE_STAGES:
            metric = getattr(self, stage)
            if not isinstance(metric, CoverageMetric):
                raise CoverageError("%s must be a CoverageMetric" % stage)
            expected = _metric(requirements, stage)
            if metric != expected:
                raise CoverageError("%s metric disagrees with requirement rows" % stage)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "api_version": self.api_version,
            "subject_hash": self.subject_hash,
            "test_plan_api_version": self.test_plan_api_version,
            "observed_case_ids": list(self.observed_case_ids),
            "requirements": [item.to_dict() for item in self.requirements],
            "planned": self.planned.to_dict(),
            "executable": self.executable.to_dict(),
            "oracle_ready": self.oracle_ready.to_dict(),
            "observed": self.observed.to_dict(),
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
    def from_dict(cls, value: Any) -> "CoverageReport":
        data = _mapping(value, "coverage report")
        fields = (
            "api_version",
            "subject_hash",
            "test_plan_api_version",
            "observed_case_ids",
            "requirements",
            "planned",
            "executable",
            "oracle_ready",
            "observed",
        )
        _strict_keys(data, fields, (), "coverage report")
        return cls(
            api_version=data["api_version"],
            subject_hash=data["subject_hash"],
            test_plan_api_version=data["test_plan_api_version"],
            observed_case_ids=_sequence(
                data["observed_case_ids"], "coverage report observed_case_ids"
            ),
            requirements=tuple(
                RequirementCoverage.from_dict(item)
                for item in _sequence(
                    data["requirements"], "coverage report requirements"
                )
            ),
            planned=CoverageMetric.from_dict(data["planned"]),
            executable=CoverageMetric.from_dict(data["executable"]),
            oracle_ready=CoverageMetric.from_dict(data["oracle_ready"]),
            observed=CoverageMetric.from_dict(data["observed"]),
        )

    @classmethod
    def from_json(cls, value: str) -> "CoverageReport":
        try:
            decoded = json.loads(
                value,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_object,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise CoverageError("invalid coverage report JSON") from exc
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


def _metric(
    requirements: Sequence[RequirementCoverage], stage: str
) -> CoverageMetric:
    covered_rows = [item for item in requirements if getattr(item, stage)]
    return CoverageMetric(
        covered=len(covered_rows),
        total=len(requirements),
        covered_weight=sum(item.risk_weight for item in covered_rows),
        total_weight=sum(item.risk_weight for item in requirements),
    )


def _case_map(cases: Sequence[CaseDraft]) -> Dict[str, CaseDraft]:
    return {item.id: item for item in cases}


def build_coverage_report(
    plan: TestPlan,
    *,
    observed_case_ids: Iterable[str] = (),
    observed_requirement_ids: Iterable[str] = (),
) -> CoverageReport:
    """Build coverage from a TestPlan and explicit dynamic evidence.

    ``observed_case_ids`` is the preferred input because it keeps the dynamic
    claim linked to a concrete executable Case. ``observed_requirement_ids``
    supports replay/import systems that already preserve requirement IDs, but
    those requirements must still have an executable planned Case.
    """

    if not isinstance(plan, TestPlan):
        raise CoverageError("plan must be a TestPlan")
    observed_cases = tuple(
        sorted(_string_tuple(observed_case_ids, "observed_case_ids"))
    )
    observed_requirements = set(
        _string_tuple(observed_requirement_ids, "observed_requirement_ids")
    )
    _ensure_unique(observed_cases, "observed case id")
    case_by_id = _case_map(plan.cases)
    unknown_cases = set(observed_cases).difference(case_by_id)
    if unknown_cases:
        raise CoverageError(
            "observed cases are not in the plan: %s"
            % ", ".join(sorted(unknown_cases))
        )
    non_executable = [
        case_id for case_id in observed_cases if not case_by_id[case_id].executable
    ]
    if non_executable:
        raise CoverageError(
            "cannot mark non-executable cases observed: %s"
            % ", ".join(sorted(non_executable))
        )
    requirement_ids = {item.id for item in plan.requirements}
    unknown_requirements = observed_requirements.difference(requirement_ids)
    if unknown_requirements:
        raise CoverageError(
            "observed requirements are not in the plan: %s"
            % ", ".join(sorted(unknown_requirements))
        )
    for case_id in observed_cases:
        observed_requirements.update(case_by_id[case_id].requirement_ids)

    gap_reasons = {}  # type: Dict[str, list]
    for gap in plan.runtime_gaps:
        gap_reasons.setdefault(gap.requirement_id, []).append(
            "%s:%s" % (gap.gap_type, gap.capability)
        )
    for requirement_id in plan.budget_excluded_requirement_ids:
        gap_reasons.setdefault(requirement_id, []).append("budget_excluded")
    for requirement_id in plan.uncovered_requirement_ids:
        gap_reasons.setdefault(requirement_id, []).append("no_selected_case")

    rows = []
    for requirement in plan.requirements:
        linked = [
            case for case in plan.cases if requirement.id in case.requirement_ids
        ]
        executable = [case for case in linked if case.executable]
        oracle_ready = [case for case in executable if case.oracle_ready]
        dynamic = [
            case
            for case in executable
            if case.id in set(observed_cases)
        ]
        if requirement.id in observed_requirements and not executable:
            raise CoverageError(
                "cannot mark requirement %s observed without an executable planned case"
                % requirement.id
            )
        rows.append(
            RequirementCoverage(
                requirement_id=requirement.id,
                capability_id=requirement.capability_id,
                priority=requirement.priority,
                risk_weight=requirement.risk_weight,
                case_ids=tuple(case.id for case in linked),
                executable_case_ids=tuple(case.id for case in executable),
                oracle_ready_case_ids=tuple(case.id for case in oracle_ready),
                observed_case_ids=tuple(case.id for case in dynamic),
                direct_observation=(
                    requirement.id in observed_requirements and not dynamic
                ),
                planned=bool(linked),
                executable=bool(executable),
                oracle_ready=bool(oracle_ready),
                observed=bool(dynamic) or requirement.id in observed_requirements,
                gap_reasons=tuple(sorted(set(gap_reasons.get(requirement.id, ())))),
            )
        )
    rows_tuple = tuple(rows)
    return CoverageReport(
        subject_hash=plan.subject_hash,
        test_plan_api_version=plan.api_version,
        observed_case_ids=observed_cases,
        requirements=rows_tuple,
        planned=_metric(rows_tuple, "planned"),
        executable=_metric(rows_tuple, "executable"),
        oracle_ready=_metric(rows_tuple, "oracle_ready"),
        observed=_metric(rows_tuple, "observed"),
    )


calculate_coverage = build_coverage_report


__all__ = [
    "COVERAGE_REPORT_API_VERSION",
    "COVERAGE_STAGES",
    "CoverageError",
    "CoverageMetric",
    "CoverageReport",
    "RequirementCoverage",
    "build_coverage_report",
    "calculate_coverage",
]
