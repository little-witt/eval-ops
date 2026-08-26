"""Deterministic control-plane contracts for the V2 evaluation Kernel.

The language model may supply semantic grades, explanations, and patch
proposals, but it never decides evidence validity, hard regressions, candidate
promotion, or convergence.  This module is intentionally independent from the
desktop client and from any specific Agent platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .environment_contracts import canonical_hash


EVIDENCE_VALIDITY_API_VERSION = "aceval.evidence-validity/v2"
ATTEMPT_VERDICT_API_VERSION = "aceval.attempt-verdict/v2"
CASE_AGGREGATE_API_VERSION = "aceval.case-aggregate/v2"
DIAGNOSIS_GRAPH_API_VERSION = "aceval.diagnosis-graph/v2"
CANDIDATE_COMPARISON_API_VERSION = "aceval.candidate-comparison/v2"
CONVERGENCE_STATE_API_VERSION = "aceval.convergence-state/v2"

_STATUSES = frozenset(("pass", "fail", "not_evaluable"))
_VALIDITY = frozenset(("valid", "invalid", "partial"))
_REASON_TOKEN = re.compile(r"[^a-z0-9._-]+")


class KernelV2Error(ValueError):
    """A V2 control-plane record is malformed or internally inconsistent."""


def _strict(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise KernelV2Error("%s must be strict JSON" % label) from exc


def _text(value: Any, label: str, maximum: int = 8192) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum or "\x00" in value:
        raise KernelV2Error("%s must be a trimmed non-empty string" % label)
    return value


def _score(value: Optional[float], label: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
        raise KernelV2Error("%s must be null or a finite number between 0 and 1" % label)
    return float(value)


@dataclass(frozen=True)
class EvidenceValidity:
    status: str
    scope: str
    reason_codes: Tuple[str, ...] = ()
    missing_channels: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    api_version: str = EVIDENCE_VALIDITY_API_VERSION

    def __post_init__(self) -> None:
        if self.api_version != EVIDENCE_VALIDITY_API_VERSION:
            raise KernelV2Error("unsupported evidence validity version")
        if self.status not in _VALIDITY:
            raise KernelV2Error("unsupported evidence validity status")
        if self.scope not in ("attempt", "dimension"):
            raise KernelV2Error("evidence validity scope must be attempt or dimension")
        object.__setattr__(self, "reason_codes", tuple(_text(item, "reason_codes[]", 256) for item in self.reason_codes))
        object.__setattr__(self, "missing_channels", tuple(_text(item, "missing_channels[]", 128) for item in self.missing_channels))
        object.__setattr__(self, "evidence_refs", tuple(_text(item, "evidence_refs[]", 4096) for item in self.evidence_refs if item))

    @property
    def evaluable(self) -> bool:
        return self.status != "invalid"

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "status": self.status,
            "scope": self.scope,
            "evaluable": self.evaluable,
            "reason_codes": list(self.reason_codes),
            "missing_channels": list(self.missing_channels),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class DimensionGrade:
    dimension: str
    status: str
    score: Optional[float]
    hard: bool
    grader_id: str
    reason: str
    evidence_refs: Tuple[str, ...] = ()
    source: str = "fact"

    def __post_init__(self) -> None:
        _text(self.dimension, "dimension", 128)
        if self.status not in _STATUSES:
            raise KernelV2Error("unsupported dimension status")
        normalized = _score(self.score, "dimension score")
        object.__setattr__(self, "score", normalized)
        if self.status == "not_evaluable" and normalized is not None:
            raise KernelV2Error("not_evaluable dimension cannot have a score")
        if self.status == "pass" and normalized is None:
            raise KernelV2Error("passing dimension requires a score")
        if not isinstance(self.hard, bool):
            raise KernelV2Error("dimension hard must be boolean")
        _text(self.grader_id, "grader_id", 256)
        _text(self.reason, "reason", 8192)
        if self.source not in ("fact", "inference", "human"):
            raise KernelV2Error("dimension source is invalid")
        object.__setattr__(self, "evidence_refs", tuple(str(item) for item in self.evidence_refs if item))

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "dimension": self.dimension,
            "status": self.status,
            "score": self.score,
            "hard": self.hard,
            "grader_id": self.grader_id,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "source": self.source,
        }


@dataclass(frozen=True)
class AttemptVerdict:
    case_id: str
    attempt_id: str
    run_context_hash: str
    evidence_validity: EvidenceValidity
    dimensions: Tuple[DimensionGrade, ...]
    status: str
    api_version: str = ATTEMPT_VERDICT_API_VERSION

    def __post_init__(self) -> None:
        if self.api_version != ATTEMPT_VERDICT_API_VERSION:
            raise KernelV2Error("unsupported attempt verdict version")
        _text(self.case_id, "case_id", 256)
        _text(self.attempt_id, "attempt_id", 256)
        _text(self.run_context_hash, "run_context_hash", 256)
        if self.status not in _STATUSES:
            raise KernelV2Error("unsupported attempt status")
        names = [item.dimension for item in self.dimensions]
        if len(names) != len(set(names)):
            raise KernelV2Error("attempt dimensions must be unique")
        if not self.evidence_validity.evaluable and self.status != "not_evaluable":
            raise KernelV2Error("invalid evidence requires not_evaluable attempt")
        if any(item.hard and item.status == "fail" for item in self.dimensions) and self.status != "fail":
            raise KernelV2Error("hard dimension failure requires failed attempt")

    @property
    def hard_pass(self) -> bool:
        return self.evidence_validity.evaluable and all(
            item.status == "pass" for item in self.dimensions if item.hard
        )

    @property
    def score(self) -> Optional[float]:
        values = [item.score for item in self.dimensions if item.score is not None]
        return sum(values) / len(values) if values else None

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "case_id": self.case_id,
            "attempt_id": self.attempt_id,
            "run_context_hash": self.run_context_hash,
            "evidence_validity": self.evidence_validity.to_dict(),
            "dimensions": [item.to_dict() for item in self.dimensions],
            "status": self.status,
            "hard_pass": self.hard_pass,
            "score": self.score,
        }


@dataclass(frozen=True)
class CaseAggregate:
    case_id: str
    required_k: int
    attempts: Tuple[AttemptVerdict, ...]
    api_version: str = CASE_AGGREGATE_API_VERSION

    def __post_init__(self) -> None:
        if self.api_version != CASE_AGGREGATE_API_VERSION:
            raise KernelV2Error("unsupported case aggregate version")
        _text(self.case_id, "case_id", 256)
        if not isinstance(self.required_k, int) or isinstance(self.required_k, bool) or not 1 <= self.required_k <= 20:
            raise KernelV2Error("required_k must be between 1 and 20")
        if not self.attempts or any(item.case_id != self.case_id for item in self.attempts):
            raise KernelV2Error("case aggregate attempts must belong to the case")

    @property
    def evaluable_attempts(self) -> Tuple[AttemptVerdict, ...]:
        return tuple(item for item in self.attempts if item.status != "not_evaluable")

    @property
    def successes(self) -> int:
        return sum(item.status == "pass" and item.hard_pass for item in self.evaluable_attempts)

    @property
    def pass_rate(self) -> Optional[float]:
        return self.successes / len(self.evaluable_attempts) if self.evaluable_attempts else None

    @property
    def pass_at_k(self) -> Optional[float]:
        return None if self.pass_rate is None else 1.0 - (1.0 - self.pass_rate) ** self.required_k

    @property
    def pass_power_k(self) -> Optional[float]:
        return None if self.pass_rate is None else self.pass_rate ** self.required_k

    @property
    def stable_pass(self) -> bool:
        return (
            len(self.evaluable_attempts) >= self.required_k
            and self.successes == len(self.evaluable_attempts)
            and self.successes >= self.required_k
        )

    @property
    def flaky(self) -> bool:
        statuses = {item.status for item in self.evaluable_attempts}
        return "pass" in statuses and "fail" in statuses

    @property
    def status(self) -> str:
        if not self.evaluable_attempts:
            return "not_evaluable"
        if self.stable_pass:
            return "pass"
        return "fail"

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "case_id": self.case_id,
            "required_k": self.required_k,
            "attempt_count": len(self.attempts),
            "evaluable_attempt_count": len(self.evaluable_attempts),
            "successes": self.successes,
            "status": self.status,
            "stable_pass": self.stable_pass,
            "flaky": self.flaky,
            "pass_rate": self.pass_rate,
            "pass_at_k": self.pass_at_k,
            "pass_power_k": self.pass_power_k,
            "attempts": [item.to_dict() for item in self.attempts],
        }


def _validity(evidence: Mapping[str, Any]) -> EvidenceValidity:
    refs = tuple(str(item) for item in (evidence.get("artifact"),) if item)
    reasons = []
    missing = []
    if not evidence.get("artifact"):
        reasons.append("attempt.artifact_missing")
        missing.append("session_artifact")
    if evidence.get("run_status") != "completed":
        reasons.append("attempt.remote_not_completed")
    if evidence.get("error"):
        reasons.append("attempt.runtime_error")
    path = evidence.get("path_conformance")
    if isinstance(path, Mapping) and path.get("status") == "not_evaluable":
        reasons.append("attempt.trace_incomplete")
        missing.append("trace")
    return EvidenceValidity(
        status="invalid" if reasons else "valid",
        scope="attempt",
        reason_codes=tuple(reasons),
        missing_channels=tuple(missing),
        evidence_refs=refs,
    )


def build_attempt_verdict(
    case: Mapping[str, Any],
    evidence: Mapping[str, Any],
    case_result: Mapping[str, Any],
    *,
    attempt_id: str,
    run_context_hash: str,
) -> AttemptVerdict:
    """Compile one compacted Session evidence record into a deterministic verdict."""

    case_id = str(case.get("id") or case_result.get("case_id") or "")
    validity = _validity(evidence)
    refs = tuple(str(item) for item in case_result.get("evidence_refs", ()) if item)
    artifact_refs = tuple(str(item) for item in (evidence.get("artifact"),) if item)
    dimensions = []
    if validity.evaluable:
        exact = evidence.get("exact_expected_match")
        result_status = str(case_result.get("status") or "not_evaluable")
        reason = str(case_result.get("reason") or "case result")
        if exact is True:
            dimensions.append(DimensionGrade("outcome", "pass", 1.0, True, "exact_expected/v1", reason, refs or artifact_refs))
        elif exact is False:
            dimensions.append(DimensionGrade("outcome", "fail", 0.0, True, "exact_expected/v1", reason, refs or artifact_refs))
        elif result_status in _STATUSES:
            dimensions.append(DimensionGrade(
                "outcome",
                result_status,
                None if result_status == "not_evaluable" else (1.0 if result_status == "pass" else 0.0),
                False,
                "semantic_case_result/v1",
                reason,
                refs,
                source="inference",
            ))
        grounded = bool(refs) or exact is not None
        dimensions.append(DimensionGrade(
            "grounding",
            "pass" if grounded else "not_evaluable",
            1.0 if grounded else None,
            False,
            "evidence_reference/v1",
            "result is linked to immutable evidence" if grounded else "semantic result did not provide an evidence reference",
            refs or artifact_refs,
        ))
        trace_summary = evidence.get("trace")
        if isinstance(trace_summary, Mapping):
            runtime_errors = trace_summary.get("errors", ())
            has_runtime_errors = isinstance(runtime_errors, Sequence) and not isinstance(runtime_errors, (str, bytes)) and bool(runtime_errors)
            dimensions.append(DimensionGrade(
                "runtime",
                "fail" if has_runtime_errors else "pass",
                0.0 if has_runtime_errors else 1.0,
                False,
                "trace_runtime_errors/v1",
                "tool/runtime errors were observed" if has_runtime_errors else "no tool/runtime error was observed in the complete trace",
                artifact_refs,
            ))
        metadata = case.get("metadata") if isinstance(case.get("metadata"), Mapping) else {}
        token_budget = metadata.get("max_total_tokens")
        usage = evidence.get("usage") if isinstance(evidence.get("usage"), Mapping) else {}
        total_tokens = usage.get("total_tokens")
        if isinstance(token_budget, (int, float)) and not isinstance(token_budget, bool) and token_budget > 0 and isinstance(total_tokens, (int, float)) and not isinstance(total_tokens, bool):
            efficient = float(total_tokens) <= float(token_budget)
            dimensions.append(DimensionGrade(
                "efficiency",
                "pass" if efficient else "fail",
                max(0.0, min(1.0, 1.0 - max(0.0, float(total_tokens) - float(token_budget)) / float(token_budget))),
                False,
                "token_budget/v1",
                "used %s tokens against a %s token budget" % (total_tokens, token_budget),
                artifact_refs,
            ))
        path = evidence.get("path_conformance")
        if isinstance(path, Mapping):
            path_status = str(path.get("status") or "not_evaluable")
            if path_status in _STATUSES:
                raw_coverage = path.get("coverage")
                if isinstance(raw_coverage, bool) or not isinstance(raw_coverage, (int, float)):
                    raw_coverage = 1.0 if path_status == "pass" else 0.0
                dimensions.append(DimensionGrade(
                    "procedure",
                    path_status,
                    None if path_status == "not_evaluable" else max(0.0, min(1.0, float(raw_coverage))),
                    path_status == "fail",
                    "semantic_execution_path/v1",
                    str(path.get("reason") or "semantic path conformance"),
                    refs,
                ))
        status = result_status if result_status in _STATUSES else "not_evaluable"
        if any(item.hard and item.status == "fail" for item in dimensions):
            status = "fail"
    else:
        status = "not_evaluable"
    return AttemptVerdict(
        case_id=case_id,
        attempt_id=attempt_id,
        run_context_hash=run_context_hash,
        evidence_validity=validity,
        dimensions=tuple(dimensions),
        status=status,
    )


def build_case_aggregates(
    cases: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    verification_evidence: Sequence[Mapping[str, Any]] = (),
    *,
    verification_results: Optional[Mapping[str, Mapping[str, Any]]] = None,
    required_k: int = 2,
    run_context: Optional[Mapping[str, Any]] = None,
) -> Tuple[CaseAggregate, ...]:
    result_by_id = {str(item.get("case_id")): item for item in results}
    primary_by_id = {str(item.get("case_id")): item for item in evidence}
    verification_by_id = {str(item.get("case_id")): item for item in verification_evidence}
    context_hash = canonical_hash(_strict(run_context or {}, "run_context"))
    aggregates = []
    for case in cases:
        case_id = str(case.get("id") or "")
        result = result_by_id.get(case_id, {"case_id": case_id, "status": "not_evaluable", "reason": "missing case result"})
        attempts = [build_attempt_verdict(case, primary_by_id.get(case_id, {}), result, attempt_id="primary", run_context_hash=context_hash)]
        if case_id in verification_by_id:
            verification = verification_by_id[case_id]
            verification_result = dict((verification_results or {}).get(case_id, {}))
            if not verification_result:
                verification_status = result.get("verification_status")
                if verification_status in _STATUSES:
                    verification_result = {
                        "case_id": case_id,
                        "status": verification_status,
                        "reason": "stability verification: %s" % verification_status,
                        "evidence_refs": result.get("evidence_refs", ()),
                    }
                else:
                    # A completed run is not proof of a semantic pass.  Only an
                    # exact oracle can be resolved without a semantic grader.
                    exact = verification.get("exact_expected_match")
                    verification_result = {
                        "case_id": case_id,
                        "status": "pass" if exact is True else ("fail" if exact is False else "not_evaluable"),
                        "reason": "verification requires a trusted deterministic or semantic grader",
                        "evidence_refs": [verification.get("artifact")],
                    }
            attempts.append(build_attempt_verdict(case, verification, verification_result, attempt_id="verification-1", run_context_hash=context_hash))
        aggregates.append(CaseAggregate(case_id=case_id, required_k=required_k, attempts=tuple(attempts)))
    return tuple(aggregates)


def _reason_code(result: Mapping[str, Any]) -> str:
    status = str(result.get("status") or "unknown")
    reason = str(result.get("reason") or "unspecified").casefold()
    if "execution path" in reason or "路径" in reason:
        category = "procedure"
    elif "remote" in reason or "infrastructure" in reason or "runtime" in reason:
        category = "infrastructure"
    elif "expected" in reason or "预期" in reason:
        category = "outcome"
    elif "stable" in reason or "verification" in reason or "偶现" in reason:
        category = "reliability"
    else:
        category = "semantic"
    token = _REASON_TOKEN.sub("-", reason)[:72].strip("-") or "unspecified"
    return "%s.%s.%s" % (status, category, token)


def compile_diagnosis_graph(
    case_results: Sequence[Mapping[str, Any]],
    failure_clusters: Sequence[Mapping[str, Any]],
    proposed_changes: Sequence[Mapping[str, Any]],
    conflicts: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Build a stable evidence-first graph; model clusters remain inferences."""

    failed = [item for item in case_results if item.get("status") != "pass"]
    clusters = []
    assigned = set()
    for raw in failure_clusters:
        case_ids = tuple(sorted(str(item) for item in raw.get("case_ids", ()) if item))
        if not case_ids:
            continue
        assigned.update(case_ids)
        cluster_id = str(raw.get("id") or "cluster-" + canonical_hash(case_ids).split(":")[-1][:10])
        facts = [
            {
                "case_id": str(item.get("case_id")),
                "status": str(item.get("status")),
                "reason_code": _reason_code(item),
                "reason": str(item.get("reason") or ""),
                "evidence_refs": list(item.get("evidence_refs", ())),
                "source": "fact",
            }
            for item in failed if str(item.get("case_id")) in case_ids
        ]
        clusters.append({
            "id": cluster_id,
            "case_ids": list(case_ids),
            "facts": facts,
            "root_cause_hypothesis": str(raw.get("root_cause") or "unresolved"),
            "hypothesis_source": "inference",
            "skill_change_authorized": bool(raw.get("skill_change_authorized") is True and any(item["status"] == "fail" for item in facts)),
        })
    for item in failed:
        case_id = str(item.get("case_id"))
        if case_id in assigned:
            continue
        signature = _reason_code(item)
        clusters.append({
            "id": "signature-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12],
            "case_ids": [case_id],
            "facts": [{
                "case_id": case_id,
                "status": str(item.get("status")),
                "reason_code": signature,
                "reason": str(item.get("reason") or ""),
                "evidence_refs": list(item.get("evidence_refs", ())),
                "source": "fact",
            }],
            "root_cause_hypothesis": "unresolved",
            "hypothesis_source": "inference",
            "skill_change_authorized": False,
        })
    proposal_nodes = []
    for index, item in enumerate(proposed_changes):
        proposal_nodes.append({
            "id": "proposal-%03d" % (index + 1),
            "target": str(item.get("target") or ""),
            "change": str(item.get("change") or ""),
            "why": str(item.get("why") or ""),
            "case_ids": list(item.get("case_ids", ())),
            "source": "inference",
        })
    graph = {
        "api_version": DIAGNOSIS_GRAPH_API_VERSION,
        "clusters": clusters,
        "proposals": proposal_nodes,
        "conflicts": _strict(list(conflicts), "conflicts"),
    }
    graph["graph_hash"] = canonical_hash(graph)
    return graph


def _aggregate_map(value: Sequence[Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    return {str(item.get("case_id")): item for item in value if isinstance(item, Mapping)}


def compare_candidates(
    current: Sequence[Mapping[str, Any]],
    previous: Sequence[Mapping[str, Any]],
    *,
    champion_id: str,
    challenger_id: str,
    minimum_effect: float,
) -> Mapping[str, Any]:
    current_map = _aggregate_map(current)
    previous_map = _aggregate_map(previous)
    common = sorted(set(current_map).intersection(previous_map))
    regressions = []
    improvements = []
    deltas = []
    for case_id in common:
        old = previous_map[case_id]
        new = current_map[case_id]
        old_pass = bool(old.get("stable_pass"))
        new_pass = bool(new.get("stable_pass"))
        if old_pass and not new_pass:
            regressions.append(case_id)
        elif not old_pass and new_pass:
            improvements.append(case_id)
        old_rate = old.get("pass_rate")
        new_rate = new.get("pass_rate")
        if isinstance(old_rate, (int, float)) and isinstance(new_rate, (int, float)):
            deltas.append(float(new_rate) - float(old_rate))
    mean_delta = sum(deltas) / len(deltas) if deltas else 0.0
    accepted = not regressions and (bool(improvements) or mean_delta >= minimum_effect)
    reasons = []
    if regressions:
        reasons.append("critical_regression")
    if not improvements and mean_delta < minimum_effect:
        reasons.append("minimum_effect_not_met")
    result = {
        "api_version": CANDIDATE_COMPARISON_API_VERSION,
        "champion_id": champion_id,
        "challenger_id": challenger_id,
        "comparable_case_ids": common,
        "hard_regression_case_ids": regressions,
        "improved_case_ids": improvements,
        "paired_mean_delta": mean_delta,
        "minimum_effect": minimum_effect,
        "accepted": accepted,
        "reasons": reasons,
    }
    result["comparison_hash"] = canonical_hash(result)
    return result


def convergence_state(
    *,
    case_aggregates: Sequence[Mapping[str, Any]],
    failed_case_ids: Sequence[str],
    comparison: Optional[Mapping[str, Any]],
    round_number: int,
    max_rounds: int,
    recent_graph_hashes: Sequence[str],
    recent_effects: Sequence[float] = (),
    patience: int,
    minimum_effect: float = 0.0,
    budget_exhausted: bool = False,
) -> Mapping[str, Any]:
    aggregates = tuple(case_aggregates)
    stable = bool(aggregates) and all(item.get("stable_pass") is True for item in aggregates)
    reason = None
    if stable and not failed_case_ids:
        reason = "target_met"
    elif budget_exhausted:
        reason = "budget_exhausted"
    elif round_number >= max_rounds:
        reason = "maximum_rounds_reached"
    elif comparison and comparison.get("hard_regression_case_ids"):
        reason = None
    elif patience > 1 and len(recent_graph_hashes) >= patience and len(set(recent_graph_hashes[-patience:])) == 1:
        reason = "cycle_detected"
    elif patience > 0 and len(recent_effects) >= patience and all(
        float(item) < minimum_effect for item in recent_effects[-patience:]
    ):
        reason = "no_material_gain"
    result = {
        "api_version": CONVERGENCE_STATE_API_VERSION,
        "converged": reason is not None,
        "reason": reason,
        "round_number": round_number,
        "max_rounds": max_rounds,
        "minimum_effect": minimum_effect,
        "remaining_failed_case_ids": list(failed_case_ids),
        # Promotion is fail-closed: a rejected or absent comparison never
        # replaces the champion.
        "champion_protected": True,
    }
    result["state_hash"] = canonical_hash(result)
    return result


__all__ = [
    "ATTEMPT_VERDICT_API_VERSION",
    "CASE_AGGREGATE_API_VERSION",
    "CANDIDATE_COMPARISON_API_VERSION",
    "CONVERGENCE_STATE_API_VERSION",
    "DIAGNOSIS_GRAPH_API_VERSION",
    "EVIDENCE_VALIDITY_API_VERSION",
    "AttemptVerdict",
    "CaseAggregate",
    "DimensionGrade",
    "EvidenceValidity",
    "KernelV2Error",
    "build_attempt_verdict",
    "build_case_aggregates",
    "compare_candidates",
    "compile_diagnosis_graph",
    "convergence_state",
]
