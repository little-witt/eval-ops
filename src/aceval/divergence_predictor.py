"""Deterministic multi-path trajectory divergence analysis for Skill evaluation.

This module deliberately treats divergence as evidence, not as a Skill defect verdict.
Callers must combine its report with deterministic graders and failure attribution before
allowing an optimizer to modify a Skill.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Optional, Sequence, Tuple

from .contracts import TraceEvent, canonical_trace_kind
from .execution_path import ExecutionPathSpec, evaluate_trace_conformance


TRAJECTORY_FINGERPRINT_API_VERSION = "aceval.trajectory-fingerprint/v1"
DIVERGENCE_REPORT_API_VERSION = "aceval.divergence-report/v1"


class DivergenceError(ValueError):
    pass


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DivergenceError("%s must be a non-empty trimmed string" % label)
    return value


def _number(value: Any, label: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DivergenceError("%s must be a finite number when provided" % label)
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise DivergenceError("%s must be a non-negative finite number" % label)
    return result


def _event_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, TraceEvent):
        return {
            "kind": value.kind,
            "seq": value.seq,
            "timestamp": value.timestamp,
            "name": value.name,
            "payload": dict(value.payload),
            "tool": value.tool,
            "duration_ms": value.duration_ms,
            "error": value.error,
        }
    if not isinstance(value, Mapping):
        raise DivergenceError("trace events must be objects or TraceEvent values")
    return value


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _tool_token(event: Mapping[str, Any]) -> str:
    tool = event.get("tool") or event.get("name") or ""
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    return "%s:%s:%s" % (canonical_trace_kind(event), str(tool), _json_hash(payload)[:20])


def _normalized_edit_distance(first: Sequence[str], second: Sequence[str]) -> float:
    if not first and not second:
        return 0.0
    previous = list(range(len(second) + 1))
    for first_index, first_value in enumerate(first, start=1):
        current = [first_index]
        for second_index, second_value in enumerate(second, start=1):
            current.append(min(
                current[-1] + 1,
                previous[second_index] + 1,
                previous[second_index - 1] + (first_value != second_value),
            ))
        previous = current
    return previous[-1] / max(len(first), len(second))


def _mean_pairwise(values: Sequence[Sequence[str]]) -> float:
    if len(values) < 2:
        return 0.0
    distances = []
    for index, first in enumerate(values):
        for second in values[index + 1:]:
            distances.append(_normalized_edit_distance(first, second))
    return sum(distances) / len(distances)


def _normalized_variance(values: Sequence[Optional[float]]) -> float:
    observed = [item for item in values if item is not None]
    if len(observed) < 2:
        return 0.0
    mean = sum(observed) / len(observed)
    if mean == 0:
        return 0.0 if all(item == 0 for item in observed) else 1.0
    return min(1.0, math.sqrt(sum((item - mean) ** 2 for item in observed) / len(observed)) / mean)


def _outcome_disagreement(outcomes: Sequence[str]) -> float:
    if not outcomes:
        return 0.0
    return 1.0 - max(outcomes.count(value) for value in set(outcomes)) / len(outcomes)


def _first_divergence(tokens: Sequence[Sequence[str]]) -> Optional[int]:
    if not tokens:
        return None
    for index in range(max(len(item) for item in tokens)):
        values = {item[index] if index < len(item) else None for item in tokens}
        if len(values) > 1:
            return index
    return None


def _usage_total(usage: Mapping[str, Any]) -> Optional[float]:
    for key in ("total_tokens", "tokens", "total_cost", "cost"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            return float(value)
    return None


@dataclass(frozen=True)
class TrajectoryDivergenceFingerprint:
    attempt_id: str
    outcome: str
    tool_sequence: Tuple[str, ...]
    required_step_ids: Tuple[str, ...]
    conformance_status: Optional[str]
    first_violation: Optional[str]
    usage_total: Optional[float]
    trace_complete: bool
    fingerprint_hash: str
    api_version: str = TRAJECTORY_FINGERPRINT_API_VERSION

    def __post_init__(self) -> None:
        _text(self.attempt_id, "attempt_id")
        _text(self.outcome, "outcome")
        if self.api_version != TRAJECTORY_FINGERPRINT_API_VERSION:
            raise DivergenceError("unsupported trajectory fingerprint api_version")
        _number(self.usage_total, "usage_total")

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "attempt_id": self.attempt_id,
            "outcome": self.outcome,
            "tool_sequence": list(self.tool_sequence),
            "required_step_ids": list(self.required_step_ids),
            "conformance_status": self.conformance_status,
            "first_violation": self.first_violation,
            "usage_total": self.usage_total,
            "trace_complete": self.trace_complete,
            "fingerprint_hash": self.fingerprint_hash,
        }


@dataclass(frozen=True)
class DivergenceReport:
    case_id: str
    fingerprints: Tuple[TrajectoryDivergenceFingerprint, ...]
    outcome_disagreement: float
    checkpoint_divergence: float
    tool_sequence_divergence: float
    cost_variance: float
    divergence_score: float
    first_tool_divergence_index: Optional[int]
    predicted_defect_surface: str
    evidence_confidence: str
    report_hash: str
    api_version: str = DIVERGENCE_REPORT_API_VERSION

    def __post_init__(self) -> None:
        _text(self.case_id, "case_id")
        if len(self.fingerprints) < 2:
            raise DivergenceError("at least two fingerprints are required")
        if self.api_version != DIVERGENCE_REPORT_API_VERSION:
            raise DivergenceError("unsupported divergence report api_version")
        for label in ("outcome_disagreement", "checkpoint_divergence", "tool_sequence_divergence", "cost_variance", "divergence_score"):
            value = getattr(self, label)
            if not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
                raise DivergenceError("%s must be within [0, 1]" % label)
        if self.predicted_defect_surface not in ("none", "outcome", "execution_path", "tool_usage", "cost"):
            raise DivergenceError("predicted_defect_surface is invalid")
        if self.evidence_confidence not in ("insufficient", "low", "moderate", "high"):
            raise DivergenceError("evidence_confidence is invalid")

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "case_id": self.case_id,
            "fingerprints": [item.to_dict() for item in self.fingerprints],
            "outcome_disagreement": self.outcome_disagreement,
            "checkpoint_divergence": self.checkpoint_divergence,
            "tool_sequence_divergence": self.tool_sequence_divergence,
            "cost_variance": self.cost_variance,
            "divergence_score": self.divergence_score,
            "first_tool_divergence_index": self.first_tool_divergence_index,
            "predicted_defect_surface": self.predicted_defect_surface,
            "evidence_confidence": self.evidence_confidence,
            "report_hash": self.report_hash,
        }


def fingerprint_trajectory(
    attempt_id: str,
    trace: Sequence[Any],
    *,
    outcome: str,
    usage: Optional[Mapping[str, Any]] = None,
    path_spec: Optional[ExecutionPathSpec] = None,
    trace_complete: bool = True,
) -> TrajectoryDivergenceFingerprint:
    """Create a stable fingerprint from one normalized execution trace."""

    normalized_trace = tuple(_event_mapping(event) for event in trace)
    conformance = None
    if path_spec is not None:
        conformance = evaluate_trace_conformance(path_spec, normalized_trace, trace_complete=trace_complete)
    required_steps = ()
    first_violation = None
    conformance_status = None
    if conformance is not None:
        required_steps = tuple(
            item["id"] for item in conformance["steps"]
            if item["kind"] == "required" and item["observed"]
        )
        conformance_status = conformance["status"]
        if conformance["violations"]:
            first_violation = str(conformance["violations"][0]["type"])
    tool_sequence = tuple(_tool_token(event) for event in normalized_trace if canonical_trace_kind(event) == "tool_call")
    normalized_usage = usage if isinstance(usage, Mapping) else {}
    content = {
        "attempt_id": attempt_id,
        "outcome": outcome,
        "tool_sequence": tool_sequence,
        "required_step_ids": required_steps,
        "conformance_status": conformance_status,
        "first_violation": first_violation,
        "usage_total": _usage_total(normalized_usage),
        "trace_complete": trace_complete,
    }
    return TrajectoryDivergenceFingerprint(
        attempt_id=attempt_id,
        outcome=outcome,
        tool_sequence=tool_sequence,
        required_step_ids=required_steps,
        conformance_status=conformance_status,
        first_violation=first_violation,
        usage_total=_usage_total(normalized_usage),
        trace_complete=trace_complete,
        fingerprint_hash=_json_hash(content),
    )


def analyze_divergence(
    case_id: str,
    attempts: Sequence[Mapping[str, Any]],
    *,
    path_spec: Optional[ExecutionPathSpec] = None,
    weights: Tuple[float, float, float, float] = (0.35, 0.35, 0.20, 0.10),
) -> DivergenceReport:
    """Analyze independent attempts without declaring that divergence is a defect.

    Each attempt requires ``attempt_id``, ``trace`` and ``outcome``. Optional fields
    are ``usage`` and ``trace_complete``. The score is deterministic and can only
    become repair evidence after an independent attribution gate accepts it.
    """

    _text(case_id, "case_id")
    if len(attempts) < 2:
        raise DivergenceError("at least two attempts are required")
    if len(weights) != 4 or any(not isinstance(item, (int, float)) or item < 0 for item in weights):
        raise DivergenceError("weights must contain four non-negative numbers")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise DivergenceError("weights must have a positive sum")
    normalized_weights = tuple(item / total_weight for item in weights)
    fingerprints = []
    for item in attempts:
        if not isinstance(item, Mapping):
            raise DivergenceError("attempts must be objects")
        trace = item.get("trace")
        if isinstance(trace, (str, bytes)) or not isinstance(trace, Sequence):
            raise DivergenceError("attempt.trace must be an array")
        fingerprints.append(fingerprint_trajectory(
            item.get("attempt_id", ""), trace,
            outcome=item.get("outcome", ""),
            usage=item.get("usage"),
            path_spec=path_spec,
            trace_complete=bool(item.get("trace_complete", True)),
        ))
    outcome_disagreement = _outcome_disagreement([item.outcome for item in fingerprints])
    checkpoint_divergence = _mean_pairwise([item.required_step_ids for item in fingerprints])
    tool_sequence_divergence = _mean_pairwise([item.tool_sequence for item in fingerprints])
    cost_variance = _normalized_variance([item.usage_total for item in fingerprints])
    components = (outcome_disagreement, checkpoint_divergence, tool_sequence_divergence, cost_variance)
    score = sum(weight * value for weight, value in zip(normalized_weights, components))
    maximum = max(components)
    if maximum == 0:
        surface = "none"
    else:
        surface = ("outcome", "execution_path", "tool_usage", "cost")[components.index(maximum)]
    confidence = "insufficient" if len(fingerprints) < 3 else "low"
    if score >= 0.25:
        confidence = "moderate"
    if score >= 0.50:
        confidence = "high"
    report_body = {
        "case_id": case_id,
        "fingerprints": [item.to_dict() for item in fingerprints],
        "outcome_disagreement": outcome_disagreement,
        "checkpoint_divergence": checkpoint_divergence,
        "tool_sequence_divergence": tool_sequence_divergence,
        "cost_variance": cost_variance,
        "divergence_score": score,
        "first_tool_divergence_index": _first_divergence([item.tool_sequence for item in fingerprints]),
        "predicted_defect_surface": surface,
        "evidence_confidence": confidence,
    }
    return DivergenceReport(
        case_id=case_id,
        fingerprints=tuple(fingerprints),
        outcome_disagreement=outcome_disagreement,
        checkpoint_divergence=checkpoint_divergence,
        tool_sequence_divergence=tool_sequence_divergence,
        cost_variance=cost_variance,
        divergence_score=score,
        first_tool_divergence_index=report_body["first_tool_divergence_index"],
        predicted_defect_surface=surface,
        evidence_confidence=confidence,
        report_hash=_json_hash(report_body),
    )


__all__ = [
    "DIVERGENCE_REPORT_API_VERSION",
    "TRAJECTORY_FINGERPRINT_API_VERSION",
    "DivergenceError",
    "DivergenceReport",
    "TrajectoryDivergenceFingerprint",
    "analyze_divergence",
    "fingerprint_trajectory",
]
