"""Objective measurement and paired gates for Skill tuning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .contracts import (
    GradeStatus,
    MetricDirection,
    ObjectiveSpec,
    is_tool_call_event,
)


@dataclass(frozen=True)
class ObjectiveMeasurement:
    objective_id: str
    value: Optional[float]
    scenario_values: Mapping[str, float]
    coverage: float
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scenario_values", MappingProxyType(dict(self.scenario_values))
        )


@dataclass(frozen=True)
class ObjectiveComparison:
    objective_id: str
    baseline: ObjectiveMeasurement
    candidate: ObjectiveMeasurement
    raw_delta: Optional[float]
    improvement: Optional[float]
    case_regressions: Tuple[str, ...]
    meets_min_delta: bool
    meets_target: bool
    evaluable: bool

    @property
    def passed(self) -> bool:
        return (
            self.evaluable
            and self.meets_min_delta
            and self.meets_target
            and not self.case_regressions
        )


def measure_objective(run: Any, objective: ObjectiveSpec) -> ObjectiveMeasurement:
    values: Dict[str, float] = {}
    for scenario in run.scenarios:
        value = _scenario_value(scenario, objective)
        if value is not None:
            values[scenario.scenario_id] = value
    total = len(run.scenarios)
    coverage = len(values) / total if total else 0.0
    if not values:
        status = "not_measured"
        aggregate = None
    elif len(values) != total:
        status = "partial"
        aggregate = _aggregate(tuple(values.values()), objective.aggregation)
    else:
        status = "measured"
        aggregate = _aggregate(tuple(values.values()), objective.aggregation)
    return ObjectiveMeasurement(
        objective_id=objective.id,
        value=aggregate,
        scenario_values=values,
        coverage=coverage,
        status=status,
    )


def compare_objective(
    baseline_run: Any, candidate_run: Any, objective: ObjectiveSpec
) -> ObjectiveComparison:
    baseline = measure_objective(baseline_run, objective)
    candidate = measure_objective(candidate_run, objective)
    evaluable = (
        baseline.status == "measured"
        and candidate.status == "measured"
        and set(baseline.scenario_values) == set(candidate.scenario_values)
        and baseline.value is not None
        and candidate.value is not None
    )
    if not evaluable:
        return ObjectiveComparison(
            objective_id=objective.id,
            baseline=baseline,
            candidate=candidate,
            raw_delta=None,
            improvement=None,
            case_regressions=(),
            meets_min_delta=False,
            meets_target=False,
            evaluable=False,
        )

    raw_delta = candidate.value - baseline.value
    improvement = (
        raw_delta
        if objective.direction == MetricDirection.MAXIMIZE
        else -raw_delta
    )
    regressions = []
    for scenario_id in sorted(baseline.scenario_values):
        old = baseline.scenario_values[scenario_id]
        new = candidate.scenario_values[scenario_id]
        case_improvement = (
            new - old
            if objective.direction == MetricDirection.MAXIMIZE
            else old - new
        )
        if case_improvement < -objective.max_case_regression:
            regressions.append(scenario_id)
    target = objective.target
    meets_target = target is None or (
        candidate.value >= target
        if objective.direction == MetricDirection.MAXIMIZE
        else candidate.value <= target
    )
    return ObjectiveComparison(
        objective_id=objective.id,
        baseline=baseline,
        candidate=candidate,
        raw_delta=raw_delta,
        improvement=improvement,
        case_regressions=tuple(regressions),
        # A tuning candidate must actually improve.  ``min_delta=0`` means
        # "any positive improvement", not "an unchanged candidate passes".
        meets_min_delta=improvement > 0 and improvement >= objective.min_delta,
        meets_target=meets_target,
        evaluable=True,
    )


def _scenario_value(scenario: Any, objective: ObjectiveSpec) -> Optional[float]:
    source = objective.source
    if source.type in ("grader_score", "grader_metric"):
        grade = next(
            (
                item
                for item in scenario.grades
                if item.grader_id == source.grader_id
            ),
            None,
        )
        if grade is None or grade.status not in (
            GradeStatus.PASS,
            GradeStatus.FAIL,
        ):
            return None
        value = (
            grade.score
            if source.type == "grader_score"
            else grade.metrics.get(str(source.key))
        )
        return _finite_number(value)
    if source.type == "scenario":
        return _finite_number(
            scenario.duration_seconds if source.key == "duration_seconds" else None
        )
    observation = scenario.observation
    if observation is None:
        return None
    if source.type == "trace_count":
        if source.key != "tool_call":
            return None
        return float(sum(is_tool_call_event(item) for item in observation.trace))
    if source.type == "usage":
        return _usage_value(observation.usage, str(source.key))
    return None


def _usage_value(usage: Mapping[str, Any], key: str) -> Optional[float]:
    if key == "input_tokens":
        return _maximum(usage, ("input_tokens", "prompt_tokens"))
    if key == "output_tokens":
        return _maximum(usage, ("output_tokens", "completion_tokens"))
    if key == "cost_usd":
        return _maximum(usage, ("cost_usd", "total_cost_usd"))
    if key == "total_tokens":
        total = _maximum(usage, ("total_tokens",))
        input_value = _maximum(usage, ("input_tokens", "prompt_tokens"))
        output_value = _maximum(usage, ("output_tokens", "completion_tokens"))
        component_total = (
            input_value + output_value
            if input_value is not None and output_value is not None
            else None
        )
        values = [item for item in (total, component_total) if item is not None]
        return max(values) if values else None
    return None


def _maximum(usage: Mapping[str, Any], keys: Sequence[str]) -> Optional[float]:
    values = [
        value
        for value in (_finite_number(usage.get(key)) for key in keys)
        if value is not None and value >= 0
    ]
    return max(values) if values else None


def _finite_number(value: Any) -> Optional[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _aggregate(values: Sequence[float], aggregation: str) -> float:
    if aggregation == "sum":
        return float(sum(values))
    return float(sum(values) / len(values))


__all__ = [
    "ObjectiveComparison",
    "ObjectiveMeasurement",
    "compare_objective",
    "measure_objective",
]
