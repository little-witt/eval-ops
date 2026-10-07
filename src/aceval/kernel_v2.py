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
from pathlib import Path
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
        hard_dimensions = tuple(item for item in self.dimensions if item.hard)
        # Match the orchestrator's hard-pass contract: an empty hard-grade
        # set is not proof of success (``all(())`` would otherwise make an
        # exploratory/model-only result look like a trusted pass).
        return self.evidence_validity.status == "valid" and bool(hard_dimensions) and all(
            item.status == "pass" for item in hard_dimensions
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
        # Outcome and stability are separate facts. A successful first
        # attempt passes immediately; ``stable_pass`` records whether the
        # configured repeat budget has also been satisfied.
        if self.successes == len(self.evaluable_attempts):
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
    partial = []
    trace = evidence.get("trace") if isinstance(evidence.get("trace"), Mapping) else {}
    # A remote terminal/error flag can be emitted after the Agent already
    # fetched the Case branch, mounted the Skill and produced a complete
    # review trace.  That is an analyzable execution outcome (often an
    # Agent/tool failure), not an evidence gap that should force a replay.
    execution_observed = (
        evidence.get("trace_complete") is True
        and int(trace.get("event_count") or 0) > 0
        and int(evidence.get("output_chars") or 0) > 0
    )
    if not evidence.get("artifact"):
        reasons.append("attempt.artifact_missing")
        missing.append("session_artifact")
    if evidence.get("run_status") != "completed":
        if execution_observed:
            partial.append("attempt.remote_not_completed_after_trace")
        else:
            reasons.append("attempt.remote_not_completed")
    if evidence.get("error"):
        if execution_observed:
            partial.append("attempt.runtime_error_after_trace")
        else:
            reasons.append("attempt.runtime_error")
    binding_status = evidence.get("binding_status")
    if binding_status == "failed":
        reasons.append("attempt.case_session_binding_failed")
        missing.append("repository_binding")
    elif binding_status == "pending":
        partial.append("attempt.case_session_binding_pending")
        missing.append("repository_binding")
    completeness = evidence.get("log_completeness")
    if isinstance(completeness, Mapping):
        completeness_reasons = [str(item) for item in completeness.get("reason_codes", ()) if item]
        complete_flag = completeness.get("complete")
        if complete_flag is not None and not isinstance(complete_flag, bool):
            completeness_reasons.append("completeness_flag_untyped")
        legacy_fixture = completeness.get("legacy_event_log") is True and str(evidence.get("source") or "") not in {"catx_session_api", "catx"}
        if legacy_fixture:
            # Synthetic/local gateways may not expose the CATX event endpoint;
            # their explicit legacy marker is informative, not a failure of
            # the local exploratory run.  Production CATX data never takes
            # this branch.
            completeness_reasons = [item for item in completeness_reasons if item != "legacy_event_log"]
        # A production receipt must carry both the original event array and
        # the hash calculated over that exact array.  Do not let a stale
        # ``complete: true`` flag override these concrete omissions.
        production_source = str(evidence.get("source") or "") in {"catx_session_api", "catx"}
        if production_source and completeness.get("legacy_event_log") is not True:
            if completeness.get("raw_events") is not True and "event_log_missing" not in completeness_reasons:
                completeness_reasons.append("event_log_missing")
            if not isinstance(completeness.get("event_log_sha256"), str) or not completeness.get("event_log_sha256"):
                if "event_log_hash_missing" not in completeness_reasons:
                    completeness_reasons.append("event_log_hash_missing")
        if complete_flag is False and not completeness_reasons:
            # A bare ``complete: false`` is still a failed integrity claim;
            # do not let an empty reason list accidentally make the attempt
            # appear valid.
            completeness_reasons.append("incomplete")
        if complete_flag is False or completeness_reasons:
            reasons.extend("attempt.log_%s" % item for item in completeness_reasons)
            if completeness.get("missing_required_event_types"):
                reasons.append("attempt.required_event_missing")
            missing.append("complete_event_log")
        if production_source and completeness.get("legacy_event_log") is True:
            # Legacy CATX-shaped data is useful for inspection but must not be
            # used as a trustworthy attempt for candidate authorization.
            partial.append("attempt.legacy_event_log")
            missing.append("immutable_event_log")
    path = evidence.get("path_conformance")
    if (
        isinstance(path, Mapping)
        and path.get("status") == "not_evaluable"
        # A native V3 path evaluator may be unavailable while the session
        # itself is complete. That is an analysis capability gap, not missing
        # execution evidence; keep the Case scoreable and surface the
        # procedure dimension as an explicit limitation.
        and evidence.get("trace_complete") is not True
    ):
        partial.append("attempt.trace_incomplete")
        missing.append("trace")
    return EvidenceValidity(
        status="invalid" if reasons else "partial" if partial else "valid",
        scope="attempt",
        reason_codes=tuple(reasons + partial),
        missing_channels=tuple(missing),
        evidence_refs=refs,
    )


def _has_grounded_reference(refs: Sequence[str], artifact_refs: Sequence[str]) -> bool:
    """Return whether a semantic citation points at this immutable attempt.

    Models may add a fragment (for example ``artifact.json#events[3]``) to
    deep-link a finding, but a free-standing path or prose token is not proof
    that the cited evidence belongs to the current attempt.
    """

    for reference in refs:
        value = str(reference).strip()
        if not value:
            continue
        for artifact in artifact_refs:
            anchor = str(artifact).strip()
            basename = Path(anchor).name if anchor else ""
            if anchor and (
                value == anchor
                or value.startswith(anchor + "#")
                # The local grader intentionally receives a compact evidence
                # envelope and commonly cites the immutable artifact by its
                # filename.  That is still an unambiguous binding inside one
                # per-Case grading request.
                or value == basename
                or value.startswith(basename + "#")
            ):
                return True
    return False


def _bounded_text(value: Any, limit: int = 320) -> str:
    """Return a compact, single-line excerpt suitable for a dimension reason."""

    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _path_reason(path: Mapping[str, Any]) -> str:
    """Explain exactly which execution-path checks failed.

    ``evaluate_trace_conformance`` intentionally keeps its core result small
    and stores details in ``steps``/``violations``.  The old caller displayed
    only ``path.reason`` (which is normally null for a failed path), making a
    hard procedure score look unexplained.  Build the human-readable reason at
    the decision boundary while retaining the raw path receipt as evidence.
    """

    violations = path.get("violations") if isinstance(path.get("violations"), Sequence) else ()
    steps = path.get("steps") if isinstance(path.get("steps"), Sequence) else ()
    labels = {
        str(item.get("id")): str(item.get("label") or item.get("id"))
        for item in steps
        if isinstance(item, Mapping) and item.get("id")
    }
    details = []
    for violation in violations:
        if not isinstance(violation, Mapping):
            continue
        kind = str(violation.get("type") or "path_violation")
        step_id = str(violation.get("step_id") or "")
        label = labels.get(step_id, step_id or "未知步骤")
        if kind == "missing_required":
            details.append("缺少必须步骤「%s」" % label)
        elif kind == "forbidden_observed":
            details.append("发生禁止步骤「%s」（Trace[%s]）" % (label, violation.get("event_index", "?")))
        elif kind == "ordering":
            details.append("步骤「%s」未按要求位于「%s」之后" % (label, labels.get(str(violation.get("after")), str(violation.get("after")))))
        elif kind == "missing_predecessor":
            details.append("步骤「%s」缺少前置步骤「%s」" % (label, labels.get(str(violation.get("after")), str(violation.get("after")))))
        elif kind == "missing_alternative":
            details.append("允许替代组「%s」没有观察到任何合法步骤" % str(violation.get("group") or "未命名"))
        else:
            details.append("步骤「%s」违反路径约束" % label)
    if details:
        return "；".join(details[:6])
    return _bounded_text(path.get("reason") or "执行路径没有通过，但未记录具体违规项")


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
        metadata = case.get("metadata") if isinstance(case.get("metadata"), Mapping) else {}
        aceval_test = metadata.get("aceval_test") if isinstance(metadata.get("aceval_test"), Mapping) else {}
        oracle_trust = aceval_test.get("oracle_trust") or metadata.get("oracle_trust")
        exact = evidence.get("exact_expected_match")
        result_status = str(case_result.get("status") or "not_evaluable")
        reason = str(case_result.get("reason") or "case result")
        formal = evidence.get("formal_grading")
        formal_grades = formal.get("grades", ()) if isinstance(formal, Mapping) else ()
        has_formal_grades = isinstance(formal_grades, Sequence) and not isinstance(formal_grades, (str, bytes)) and bool(formal_grades)
        # ``formal_grading`` is an immutable deterministic signal.  If a
        # producer claims to have run formal graders but gives us an empty or
        # malformed grade list, do not silently fall back to a semantic/exact
        # result.  That fallback could turn a broken grader receipt into a
        # passing Attempt.  Keep the malformed record visible as a hard,
        # not-evaluable dimension so the caller can request a fresh run.
        formal_status = str(formal.get("status") or "") if isinstance(formal, Mapping) else ""
        formal_present = "formal_grading" in evidence
        evalpack_mode = metadata.get("expectation_mode") == "evalpack_graders"
        evalpack_lifecycle = str(metadata.get("evalpack_lifecycle") or "")
        trusted_evalpack = (
            evalpack_lifecycle in ("frozen", "legacy")
            and (evalpack_mode or bool(metadata.get("evalpack_grader_ids")))
        )
        # A generated draft may carry an informational ``not_calibrated``
        # receipt while a user-provided exact expectation or an exploratory
        # semantic result remains independently evaluable.  Once a Case is
        # explicitly bound to a trusted EvalPack, however, the formal receipt
        # is mandatory and cannot be replaced by a softer result.  Explicitly
        # malformed formal claims (including ``status: pass`` without grades)
        # are always treated as declared and fail closed.
        informational_draft = formal_status == "not_calibrated" and not trusted_evalpack
        formal_declared = evalpack_mode or trusted_evalpack or (
            isinstance(formal, Mapping)
            and (
                formal_status in _STATUSES
                or ("grades" in formal and formal_status != "not_calibrated")
            )
        ) or (
            formal_present
            and formal is not None
            and not informational_draft
        )
        formal_contract_errors = []
        if formal_declared and not isinstance(formal, Mapping):
            formal_contract_errors.append("formal_grading must be an object")
        elif formal_declared and not has_formal_grades:
            formal_contract_errors.append("formal_grading.grades must be a non-empty array")
        elif has_formal_grades and not all(isinstance(item, Mapping) for item in formal_grades):
            formal_contract_errors.append("formal_grading.grades contains a non-object item")

        def mark_formal_contract_error(message: str) -> None:
            # Keep one stable diagnostic dimension even when several malformed
            # grades are present; duplicate dimension names would make the
            # whole verdict unparsable and hide the useful failure reason.
            if any(item.dimension == "outcome:formal_grading_contract" for item in dimensions):
                return
            dimensions.append(DimensionGrade(
                "outcome:formal_grading_contract",
                "not_evaluable",
                None,
                True,
                "formal_grading_contract/v1",
                message,
                artifact_refs,
            ))

        if formal_contract_errors:
            mark_formal_contract_error(
                "formal EvalPack Grader receipt is missing or malformed: %s" % "; ".join(formal_contract_errors)
            )
        if has_formal_grades and not formal_contract_errors:
            dimension_by_type = {
                "workspace_diff": "safety_side_effect",
                "trace_assert": "procedure",
                "source_reference": "evidence_grounding",
                "artifact_exists": "outcome",
                "json_schema": "outcome",
                "json_path": "outcome",
                "record_match": "outcome",
                "code_review_findings": "outcome",
            }
            seen_formal_dimensions = set()
            for grade in formal_grades:
                # The contract check above records malformed entries as
                # not-evaluable; skip them here to avoid raising while still
                # preserving any well-formed dimensions for diagnostics.
                if not isinstance(grade, Mapping):
                    continue
                try:
                    raw_status = str(grade.get("status") or "not_evaluable")
                    if raw_status not in _STATUSES:
                        raise ValueError("unsupported formal grade status: %s" % raw_status)
                    grade_status = raw_status
                    raw_score = grade.get("score")
                    score = (
                        float(raw_score)
                        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool)
                        else 1.0 if grade_status == "pass" else 0.0 if grade_status == "fail" else None
                    )
                    grader_id = str(grade.get("grader_id") or "evalpack-grader")
                    base_dimension = dimension_by_type.get(str(grade.get("grader_type") or ""), "outcome")
                    if "hard" in grade and not isinstance(grade.get("hard"), bool):
                        raise ValueError("formal grade hard flag must be boolean")
                    dimension = DimensionGrade(
                        "%s:%s" % (base_dimension, grader_id),
                        grade_status,
                        score,
                        bool(grade.get("hard", True)),
                        "%s/%s" % (grader_id, str(grade.get("version") or "unknown")),
                        str(grade.get("message") or "EvalPack Grader result"),
                        artifact_refs,
                    )
                    if dimension.dimension in seen_formal_dimensions:
                        mark_formal_contract_error("formal EvalPack Grader result contains duplicate dimensions")
                        break
                    seen_formal_dimensions.add(dimension.dimension)
                    dimensions.append(dimension)
                except (KernelV2Error, TypeError, ValueError) as exc:
                    mark_formal_contract_error("formal EvalPack Grader result is invalid: %s" % str(exc)[:300])
                    break
        elif not formal_declared and exact is True:
            dimensions.append(DimensionGrade("outcome", "pass", 1.0, True, "exact_expected/v1", "输出与用户确认的期望结果一致。实际输出：%s" % _bounded_text(evidence.get("output_excerpt") or "（空）"), refs or artifact_refs))
        elif not formal_declared and exact is False:
            expected = case.get("expected_output")
            expected_text = json.dumps(expected, ensure_ascii=False, sort_keys=True, default=str) if not isinstance(expected, str) else expected
            dimensions.append(DimensionGrade(
                "outcome",
                "fail",
                0.0,
                True,
                "exact_expected/v1",
                "输出与用户确认的期望结果不一致。期望：%s；实际输出：%s" % (
                    _bounded_text(expected_text),
                    _bounded_text(evidence.get("output_excerpt") or "（空）"),
                ),
                refs or artifact_refs,
            ))
        elif not formal_declared and result_status in _STATUSES:
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
        exploratory_oracle = (
            metadata.get("expectation_mode") == "model_proposed"
            or oracle_trust in ("model_proposed", "unobservable")
        )
        calibrated_semantic = (
            metadata.get("expectation_mode") == "semantic"
            and aceval_test.get("oracle_trust") == "human_confirmed"
            and aceval_test.get("oracle_ready") is True
        )
        # Keep the historical free-form citation behavior for uncalibrated
        # exploratory/compatibility records, while requiring a citation that
        # actually anchors this immutable artifact for human-confirmed
        # semantic judgments.
        grounded = (
            exact is not None
            or _has_grounded_reference(refs, artifact_refs)
            if calibrated_semantic
            else bool(refs) or exact is not None
        )
        # A semantic pass without an explicit evidence reference is not safe
        # to use as proof. Explicitly exploratory/model-proposed Cases remain
        # visible and backwards-compatible: they cannot authorize a Skill edit
        # via ``_optimization_case_ready`` and do not silently become a trusted
        # Oracle. Deterministic exact and EvalPack results already carry their
        # own hard evidence.
        grounding_hard = (
            result_status == "pass"
            and exact is None
            and not has_formal_grades
            and not exploratory_oracle
        )
        execution_observed = (
            evidence.get("trace_complete") is True
            and int(evidence.get("output_chars") or 0) > 0
        )
        grounding_status = "pass" if grounded else "fail" if execution_observed else "not_evaluable"
        grounding_score = 1.0 if grounded else 0.0 if execution_observed else None
        dimensions.append(DimensionGrade(
            "grounding",
            grounding_status,
            grounding_score,
            grounding_hard,
            "evidence_reference/v1",
            (
                "结果已绑定本次不可变会话证据（引用：%s）" % "、".join(refs or artifact_refs)
                if grounded
                else "语义结果未引用本次不可变会话证据；已收到输出/Trace，但没有可追溯引用"
            ),
            refs or artifact_refs,
        ))
        trace_summary = evidence.get("trace")
        if isinstance(trace_summary, Mapping):
            runtime_errors = trace_summary.get("errors", ())
            has_runtime_errors = (
                isinstance(runtime_errors, Sequence)
                and not isinstance(runtime_errors, (str, bytes))
                and bool(runtime_errors)
            ) or bool(evidence.get("error"))
            dimensions.append(DimensionGrade(
                "runtime",
                "fail" if has_runtime_errors else "pass",
                0.0 if has_runtime_errors else 1.0,
                False,
                "trace_runtime_errors/v1",
                (
                    "Trace 观察到运行时/工具错误：%s" % "；".join(
                        _bounded_text(item.get("error"), 220)
                        for item in runtime_errors
                        if isinstance(item, Mapping) and item.get("error")
                    )
                    if has_runtime_errors
                    else "完整 Trace 中未观察到工具或运行时错误（共 %s 个事件）" % trace_summary.get("event_count", 0)
                ),
                artifact_refs,
            ))
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
                "实际使用 %s tokens；预算 %s tokens，%s" % (total_tokens, token_budget, "未超预算" if efficient else "超过预算"),
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
                    _path_reason(path) if path_status == "fail" else _bounded_text(path.get("reason") or "执行路径中的必须步骤均已观察到"),
                    tuple(
                        "%s#trace[%s]" % (artifact_refs[0], index)
                        for step in (path.get("steps") or ())
                        if isinstance(step, Mapping)
                        for index in (step.get("evidence_event_indexes") or ())
                        if artifact_refs
                    ) or refs or artifact_refs,
                ))
        status = result_status if result_status in _STATUSES else "not_evaluable"
        if any(item.hard and item.status == "fail" for item in dimensions):
            status = "fail"
        elif any(item.hard and item.status == "not_evaluable" for item in dimensions):
            status = "not_evaluable"
        # ``partial`` evidence remains evaluable: it records a terminal or
        # runtime anomaly after a complete trace, which is precisely the
        # execution effect the analysis phase must attribute.  Only an
        # ``invalid`` attempt (missing/contradictory immutable evidence)
        # forces the aggregate into ``not_evaluable``.
        elif not validity.evaluable:
            status = "not_evaluable"
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

    def case_evidence_refs(item: Mapping[str, Any]) -> list[str]:
        refs = [str(ref) for ref in item.get("evidence_refs", ()) if str(ref)]
        for dimension in item.get("dimension_summaries", ()) if isinstance(item.get("dimension_summaries"), Sequence) else ():
            if isinstance(dimension, Mapping):
                refs.extend(str(ref) for ref in dimension.get("evidence_refs", ()) if str(ref))
        for attempt in item.get("attempts", ()) if isinstance(item.get("attempts"), Sequence) else ():
            if not isinstance(attempt, Mapping):
                continue
            for dimension in attempt.get("dimensions", ()) if isinstance(attempt.get("dimensions"), Sequence) else ():
                if isinstance(dimension, Mapping):
                    refs.extend(str(ref) for ref in dimension.get("evidence_refs", ()) if str(ref))
        return list(dict.fromkeys(refs))[:12]

    def failure_dimensions(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        source = item.get("dimension_summaries")
        if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
            source = [
                dimension
                for attempt in item.get("attempts", ()) if isinstance(attempt, Mapping)
                for dimension in attempt.get("dimensions", ()) if isinstance(dimension, Mapping)
            ]
        result = []
        seen = set()
        for dimension in source:
            if not isinstance(dimension, Mapping):
                continue
            status = str(dimension.get("status") or "not_evaluable")
            if status not in ("fail", "not_evaluable"):
                continue
            name = str(dimension.get("dimension") or "unknown")
            if name in seen:
                continue
            seen.add(name)
            result.append({
                "dimension": name,
                "label": str(dimension.get("label") or name.replace("_", " ")),
                "status": status,
                "score": dimension.get("score"),
                "hard": dimension.get("hard") is True,
                "reason": str(dimension.get("summary_reason") or dimension.get("reason") or dimension.get("evidence_detail") or "该维度未达到通过标准"),
                "evidence_detail": str(dimension.get("summary_reason") or dimension.get("evidence_detail") or ""),
                "evidence_refs": [str(ref) for ref in dimension.get("evidence_refs", ()) if str(ref)],
            })
        return result

    def fact_for(item: Mapping[str, Any]) -> Mapping[str, Any]:
        dimensions = failure_dimensions(item)
        missing = [
            str(value)
            for value in (item.get("goal_observations", {}) or {}).get("missing_requirements", ())
            if str(value)
        ] if isinstance(item.get("goal_observations"), Mapping) else []
        fragments = []
        compact_summary = str(item.get("summary_fact") or "").strip()
        if compact_summary:
            fragments.append(compact_summary)
        for dimension in dimensions:
            if compact_summary:
                continue
            detail = dimension.get("evidence_detail") or dimension.get("reason")
            score = dimension.get("score")
            score_text = "" if score is None else "（评分 %.0f%%）" % (float(score) * 100)
            fragments.append("%s%s：%s" % (dimension.get("label"), score_text, detail))
        if missing:
            fragments.append("目标未观察到：%s" % "、".join(missing[:5]))
        summary = "；".join(fragment for fragment in fragments if fragment)
        if not summary:
            summary = str(item.get("reason") or "该 Case 未达到一个或多个评测目标")
        return {
            "case_id": str(item.get("case_id")),
            "status": str(item.get("status") or "not_evaluable"),
            "reason_code": _reason_code(item),
            "reason": summary,
            "failure_dimensions": dimensions,
            "goal_gaps": missing,
            "evidence_refs": case_evidence_refs(item),
            "source": "fact",
        }

    failed = [item for item in case_results if item.get("status") != "pass"]
    clusters = []
    assigned = set()
    cluster_by_cases = {}
    for raw in failure_clusters:
        case_ids = tuple(sorted(str(item) for item in raw.get("case_ids", ()) if item))
        if not case_ids:
            continue
        assigned.update(case_ids)
        cluster_key = tuple(case_ids)
        cluster_id = str(raw.get("id") or "cluster-" + canonical_hash(case_ids).split(":")[-1][:10])
        facts = [fact_for(item) for item in failed if str(item.get("case_id")) in case_ids]
        value = {
            "id": cluster_id,
            "case_ids": list(case_ids),
            "facts": facts,
            "root_cause_hypothesis": str(raw.get("root_cause_hypothesis") or raw.get("root_cause") or raw.get("hypothesis") or "unresolved"),
            "hypothesis_source": "inference",
            "skill_change_authorized": bool(raw.get("skill_change_authorized") is True and any(item["status"] == "fail" for item in facts)),
            "responsibility": str(raw.get("responsibility") or raw.get("classification") or raw.get("cause_type") or ""),
            "skill_factors": list(raw.get("skill_factors", ())) if isinstance(raw.get("skill_factors"), Sequence) and not isinstance(raw.get("skill_factors"), (str, bytes)) else [],
            "agent_model_factors": list(raw.get("agent_model_factors", raw.get("model_factors", ()))) if isinstance(raw.get("agent_model_factors", raw.get("model_factors", ())), Sequence) and not isinstance(raw.get("agent_model_factors", raw.get("model_factors", ())), (str, bytes)) else [],
            "environment_factors": list(raw.get("environment_factors", raw.get("tool_factors", ()))) if isinstance(raw.get("environment_factors", raw.get("tool_factors", ())), Sequence) and not isinstance(raw.get("environment_factors", raw.get("tool_factors", ())), (str, bytes)) else [],
        }
        existing = cluster_by_cases.get(cluster_key)
        if existing is not None:
            # Model attribution can emit the same Case set twice (for
            # example once as a Skill issue and once as an evaluator issue).
            # Merge those hypotheses into one evidence card instead of
            # rendering duplicate blocks with identical facts.
            hypotheses = [str(existing.get("root_cause_hypothesis") or ""), str(value.get("root_cause_hypothesis") or "")]
            existing["root_cause_hypothesis"] = "；".join(dict.fromkeys(item for item in hypotheses if item and item != "unresolved")) or "unresolved"
            existing["skill_change_authorized"] = bool(existing.get("skill_change_authorized") or value.get("skill_change_authorized"))
            known = {str(item.get("case_id")): item for item in existing.get("facts", ()) if isinstance(item, Mapping)}
            for fact in value.get("facts", ()):
                if isinstance(fact, Mapping):
                    known.setdefault(str(fact.get("case_id")), dict(fact))
            existing["facts"] = list(known.values())
        else:
            clusters.append(value)
            cluster_by_cases[cluster_key] = value
    for item in failed:
        case_id = str(item.get("case_id"))
        if case_id in assigned:
            continue
        signature = _reason_code(item)
        clusters.append({
            "id": "signature-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12],
            "case_ids": [case_id],
            "facts": [fact_for(item)],
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
            "target_guidance": str(item.get("target_guidance") or item.get("section") or ""),
            "validation_steps": [str(step) for step in item.get("validation_steps", item.get("verification_steps", ())) if str(step)] if isinstance(item.get("validation_steps", item.get("verification_steps", ())), Sequence) and not isinstance(item.get("validation_steps", item.get("verification_steps", ())), (str, bytes)) else [],
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


def _aggregate_contract_errors(
    value: Mapping[str, Any], *, case_id: str
) -> Tuple[str, ...]:
    """Validate the minimum immutable shape used by candidate comparison.

    ``compare_candidates`` is an authorization boundary.  It must not trust
    a producer-provided ``stable_pass``/``pass_rate`` when the underlying
    attempts or hard dimensions are absent, malformed, or contradictory.
    Older records may omit optional manifest fields, but they still need a
    complete attempt/evidence receipt before they can be used as a paired
    baseline.
    """

    errors = []
    if not isinstance(value.get("stable_pass"), bool):
        errors.append("stable_pass must be boolean")
    status = value.get("status")
    if status is not None and status not in _STATUSES:
        errors.append("status is invalid")
    pass_rate = value.get("pass_rate")
    # A CaseAggregate legitimately reports ``null`` when every Attempt is
    # not-evaluable.  Keep that state distinct from a measured 0% result;
    # only reject null after inspecting the receipt below when evaluable
    # attempts are present.
    if pass_rate is not None and (
        isinstance(pass_rate, bool) or not isinstance(pass_rate, (int, float))
    ):
        errors.append("pass_rate must be null or a finite number")
    elif pass_rate is not None and (
        not math.isfinite(float(pass_rate)) or not 0.0 <= float(pass_rate) <= 1.0
    ):
        errors.append("pass_rate is outside 0..1")

    # Aggregates emitted before the V2 attempt receipt was introduced may
    # omit ``attempts`` entirely.  Keep those records indexable for history
    # and diagnostics, but let the caller mark them as legacy/non-promotable;
    # an explicitly supplied empty or malformed attempts array remains an
    # invalid receipt.
    legacy_without_attempts = "attempts" not in value
    attempts = value.get("attempts")
    if legacy_without_attempts:
        attempts = ()
    elif not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)) or not attempts:
        errors.append("attempts must be a non-empty array")
        attempts = ()
    else:
        attempt_ids = []
        attempt_id_fields_present = False
        for index, attempt in enumerate(attempts):
            prefix = "attempt[%d]" % index
            if not isinstance(attempt, Mapping):
                errors.append("%s must be an object" % prefix)
                continue
            # Receipts created before attempt IDs were introduced can still be
            # inspected in read-only compatibility mode.  If a producer does
            # provide one ID, however, every receipt must provide a unique,
            # non-empty ID; otherwise repeated runs could be silently merged
            # or counted twice at the authorization boundary.
            if "attempt_id" in attempt:
                attempt_id_fields_present = True
                raw_attempt_id = attempt.get("attempt_id")
                if not isinstance(raw_attempt_id, str) or not raw_attempt_id.strip():
                    errors.append("%s attempt_id must be a non-empty string" % prefix)
                else:
                    attempt_ids.append(raw_attempt_id.strip())
            attempt_case_id = attempt.get("case_id")
            if attempt_case_id is not None and str(attempt_case_id) != case_id:
                errors.append("%s case_id does not match aggregate" % prefix)
            if attempt.get("status") not in _STATUSES:
                errors.append("%s status is invalid" % prefix)
            validity = attempt.get("evidence_validity")
            if not isinstance(validity, Mapping) or validity.get("status") not in _VALIDITY:
                errors.append("%s evidence validity is missing or invalid" % prefix)
            dimensions = attempt.get("dimensions")
            if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)) or not dimensions:
                errors.append("%s dimensions must be a non-empty array" % prefix)
                continue
            names = set()
            for dimension_index, dimension in enumerate(dimensions):
                label = "%s.dimensions[%d]" % (prefix, dimension_index)
                if not isinstance(dimension, Mapping):
                    errors.append("%s must be an object" % label)
                    continue
                name = dimension.get("dimension")
                if not isinstance(name, str) or not name.strip() or name in names:
                    errors.append("%s dimension name is missing or duplicated" % label)
                else:
                    names.add(name)
                if dimension.get("status") not in _STATUSES:
                    errors.append("%s status is invalid" % label)
                if not isinstance(dimension.get("hard"), bool):
                    errors.append("%s hard must be boolean" % label)
                score = dimension.get("score")
                if score is not None and (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(float(score))
                    or not 0.0 <= float(score) <= 1.0
                ):
                    errors.append("%s score is invalid" % label)

            # ``hard_pass`` is a derived security property, not a value that
            # an aggregate producer may assert independently.  Without this
            # check a forged aggregate could set ``hard_pass: true`` while
            # carrying only soft dimensions, and candidate comparison would
            # accept it as a stable hard-gated pass.  Require at least one
            # well-formed hard dimension and verify the serialized flag
            # against the same evidence/status rule used by AttemptVerdict.
            hard_dimensions = tuple(
                item
                for item in dimensions
                if isinstance(item, Mapping) and isinstance(item.get("hard"), bool) and item.get("hard") is True
            )
            derived_hard_pass = (
                isinstance(validity, Mapping)
                and validity.get("status") == "valid"
                and bool(hard_dimensions)
                and all(item.get("status") == "pass" for item in hard_dimensions)
            )
            if not isinstance(attempt.get("hard_pass"), bool):
                errors.append("%s hard_pass must be boolean" % prefix)
            elif attempt.get("hard_pass") is not derived_hard_pass:
                errors.append("%s hard_pass does not match hard dimensions/evidence" % prefix)

            # Match the invariant enforced by ``AttemptVerdict`` when a raw
            # receipt is supplied directly.  A hard dimension failure cannot
            # be serialized as a passing attempt.
            if (
                attempt.get("status") == "pass"
                and any(
                    isinstance(item, Mapping)
                    and item.get("hard") is True
                    and item.get("status") == "fail"
                    for item in dimensions
                )
            ):
                errors.append("%s pass status conflicts with hard dimension failure" % prefix)

        if attempt_id_fields_present:
            if len(attempt_ids) != len(attempts):
                errors.append("attempt_id is missing from one or more attempts")
            if len(attempt_ids) != len(set(attempt_ids)):
                errors.append("attempt_id values must be unique")

    required_k = value.get("required_k")
    if required_k is not None and (
        isinstance(required_k, bool) or not isinstance(required_k, int) or not 1 <= required_k <= 20
    ):
        errors.append("required_k is invalid")
    for count_name in ("attempt_count", "evaluable_attempt_count", "successes"):
        count = value.get(count_name)
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            errors.append("%s is invalid" % count_name)
    if not legacy_without_attempts and isinstance(attempts, Sequence) and not isinstance(attempts, (str, bytes)):
        if value.get("attempt_count") is not None and value.get("attempt_count") != len(attempts):
            errors.append("attempt_count does not match attempts")
        evaluable_count = sum(
            isinstance(item, Mapping) and item.get("status") != "not_evaluable"
            for item in attempts
        )
        if value.get("evaluable_attempt_count") is not None and value.get("evaluable_attempt_count") != evaluable_count:
            errors.append("evaluable_attempt_count does not match attempts")
        successes = sum(
            isinstance(item, Mapping)
            and item.get("status") == "pass"
            and item.get("hard_pass") is True
            for item in attempts
        )
        evaluable_attempts = sum(
            isinstance(item, Mapping) and item.get("status") != "not_evaluable"
            for item in attempts
        )
        derived_pass_rate = (
            successes / evaluable_attempts if evaluable_attempts else None
        )
        # ``pass_rate`` is a derived receipt field.  Comparing it with the
        # Attempt records prevents a producer from claiming a large paired
        # improvement while the underlying attempts tell a different story.
        if derived_pass_rate is None:
            if pass_rate is not None:
                errors.append("pass_rate must be null when no attempt is evaluable")
        elif pass_rate is None or not math.isclose(
            float(pass_rate), float(derived_pass_rate), rel_tol=0.0, abs_tol=1e-9
        ):
            errors.append("pass_rate does not match attempt receipts")
        if value.get("successes") is not None and value.get("successes") != successes:
            errors.append("successes does not match attempts")
        required = required_k if isinstance(required_k, int) and not isinstance(required_k, bool) else None
        if required is not None:
            derived_stable = (
                evaluable_attempts >= required
                and successes == evaluable_attempts
                and successes >= required
            )
            if value.get("stable_pass") is not derived_stable:
                errors.append("stable_pass does not match attempt receipts")
            derived_status = (
                "not_evaluable"
                if not evaluable_attempts
                else "pass"
                if successes == evaluable_attempts
                else "fail"
            )
            if value.get("status") is not None and value.get("status") != derived_status:
                errors.append("status does not match attempt receipts")
        elif value.get("stable_pass") is True:
            # Without a repeat budget we cannot establish stability, even if
            # the one observed attempt passed.  Keep this as a malformed
            # authorization record rather than treating it as a stable pass.
            errors.append("stable_pass requires required_k")
        # ``status=pass`` does not imply repeat verification; callers inspect
        # ``stable_pass`` for that independent guarantee.
    return tuple(errors)


def _validated_aggregate_map(
    value: Any,
) -> Tuple[Mapping[str, Mapping[str, Any]], Tuple[str, ...], Tuple[str, ...]]:
    """Return only complete, unique aggregates and visible malformed IDs."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return {}, ("<invalid-aggregate-list>",), ()
    result: dict[str, Mapping[str, Any]] = {}
    malformed = set()
    legacy = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            malformed.add("<invalid-aggregate-%d>" % index)
            continue
        raw_id = item.get("case_id")
        case_id = str(raw_id).strip() if isinstance(raw_id, str) else ""
        if not case_id:
            malformed.add("<invalid-aggregate-%d>" % index)
            continue
        errors = _aggregate_contract_errors(item, case_id=case_id)
        if errors:
            malformed.add(case_id)
        if case_id in result:
            malformed.add(case_id)
            # Keep the first occurrence for deterministic pairing; the
            # duplicate is represented by the malformed gate above.
            continue
        # Retain malformed rows in the map for diagnostics (regressions,
        # evidence blocks, and manifest mismatches), but never authorize a
        # promotion when ``malformed`` is non-empty.
        result[case_id] = item
        if not errors and "attempts" not in item:
            legacy.add(case_id)
    return result, tuple(sorted(malformed)), tuple(sorted(legacy))


def _aggregate_map(value: Sequence[Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    """Legacy helper retained for callers that only need ID indexing."""

    return {str(item.get("case_id")): item for item in value if isinstance(item, Mapping)}


def _sequence_field(value: Mapping[str, Any], field: str) -> Tuple[Any, ...]:
    """Read a repeatable aggregate field without trusting its producer type.

    Aggregate validation records malformed fields for the fail-closed gate,
    but comparison still needs to produce diagnostics for those rows.  A
    malformed ``attempts: null`` or ``dimensions: null`` must therefore not
    crash the diagnostic path while it is being reported.
    """

    raw = value.get(field, ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(raw)


def compare_candidates(
    current: Sequence[Mapping[str, Any]],
    previous: Sequence[Mapping[str, Any]],
    *,
    champion_id: str,
    challenger_id: str,
    minimum_effect: float,
    champion_context_hash: Optional[str] = None,
    challenger_context_hash: Optional[str] = None,
) -> Mapping[str, Any]:
    current_map, current_malformed, current_legacy = _validated_aggregate_map(current)
    previous_map, previous_malformed, previous_legacy = _validated_aggregate_map(previous)
    malformed_aggregate_case_ids = sorted(
        set(current_malformed).union(previous_malformed)
    )
    legacy_aggregate_case_ids = sorted(set(current_legacy).union(previous_legacy))
    common = sorted(set(current_map).intersection(previous_map))
    challenger_only = sorted(set(current_map).difference(previous_map))
    champion_only = sorted(set(previous_map).difference(current_map))
    # Malformed rows remain in the maps for diagnostics, so a malformed Case
    # present on both sides is still visibly paired (rather than being
    # misreported as two different Case sets).  The separate malformed gate
    # below keeps the comparison fail-closed and prevents promotion.
    regressions = []
    improvements = []
    champion_evidence_blocked = []
    challenger_evidence_blocked = []
    hard_gate_failures = []
    insufficient_stability = []
    manifest_mismatches = []
    deltas = []
    dimension_values = {}
    for case_id in common:
        old = previous_map[case_id]
        new = current_map[case_id]
        old_attempts = [item for item in _sequence_field(old, "attempts") if isinstance(item, Mapping)]
        new_attempts = [item for item in _sequence_field(new, "attempts") if isinstance(item, Mapping)]
        # A paired score is meaningful only when both sides used the same
        # frozen Case revision, repeat budget, and dimension/grader contract.
        # Older aggregate records may not carry these optional manifest
        # fields; in that compatibility mode we retain the historical checks.
        case_manifest_mismatch = False
        old_revision = old.get("case_revision")
        new_revision = new.get("case_revision")
        if old_revision is not None and new_revision is not None and old_revision != new_revision:
            case_manifest_mismatch = True
        old_required_k = old.get("required_k")
        new_required_k = new.get("required_k")
        if old_required_k is not None and new_required_k is not None and old_required_k != new_required_k:
            case_manifest_mismatch = True

        def dimension_signature(attempts: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str, bool], ...]:
            values = set()
            for attempt in attempts:
                for dimension in _sequence_field(attempt, "dimensions"):
                    if not isinstance(dimension, Mapping):
                        continue
                    name = str(dimension.get("dimension") or "")
                    grader = str(dimension.get("grader_id") or "")
                    hard = dimension.get("hard") is True
                    if name:
                        values.add((name, grader, hard))
            return tuple(sorted(values))

        old_dimensions = dimension_signature(old_attempts)
        new_dimensions = dimension_signature(new_attempts)
        if old_dimensions != new_dimensions and (old_dimensions or new_dimensions):
            case_manifest_mismatch = True
        old_manifest = old.get("attempt_manifest")
        new_manifest = new.get("attempt_manifest")
        if isinstance(old_manifest, Mapping) and isinstance(new_manifest, Mapping):
            # Compare only invariant manifest keys; timestamps and session ids
            # are expected to differ between Champion and Challenger runs.
            for key in ("case_revision", "required_k", "dimension_grader_versions", "grader_versions"):
                if key in old_manifest and key in new_manifest and old_manifest.get(key) != new_manifest.get(key):
                    case_manifest_mismatch = True
                    break
        if case_manifest_mismatch:
            manifest_mismatches.append(case_id)
        if (
            old.get("status") == "not_evaluable"
            or any(
                isinstance(item.get("evidence_validity"), Mapping)
                and item.get("evidence_validity", {}).get("status") != "valid"
                for item in old_attempts
            )
        ):
            champion_evidence_blocked.append(case_id)
        if (
            new.get("status") == "not_evaluable"
            or any(
                isinstance(item.get("evidence_validity"), Mapping)
                and item.get("evidence_validity", {}).get("status") != "valid"
                for item in new_attempts
            )
        ):
            challenger_evidence_blocked.append(case_id)
        if any(
            any(
                isinstance(dimension, Mapping)
                and dimension.get("hard") is True
                and dimension.get("status") == "fail"
                for dimension in _sequence_field(item, "dimensions")
            )
            for item in new_attempts
        ):
            hard_gate_failures.append(case_id)
        old_pass = bool(old.get("stable_pass"))
        new_pass = bool(new.get("stable_pass"))
        # A Challenger is only promotable after it has satisfied its frozen
        # repeat budget.  A higher one-shot pass rate is not enough: the
        # comparison must not turn an unfinished/stochastic run into a
        # published Skill.  Keep this separate from ``hard_regression`` so
        # the UI can explain that another verification run is needed.
        if not new_pass and new.get("status") != "not_evaluable":
            insufficient_stability.append(case_id)
        if old_pass and not new_pass:
            regressions.append(case_id)
        elif not old_pass and new_pass:
            improvements.append(case_id)
        old_rate = old.get("pass_rate")
        new_rate = new.get("pass_rate")
        if isinstance(old_rate, (int, float)) and isinstance(new_rate, (int, float)):
            deltas.append(float(new_rate) - float(old_rate))
        for side, aggregate in (("champion", old), ("challenger", new)):
            per_dimension = {}
            for attempt in _sequence_field(aggregate, "attempts"):
                if not isinstance(attempt, Mapping):
                    continue
                for dimension in _sequence_field(attempt, "dimensions"):
                    if not isinstance(dimension, Mapping):
                        continue
                    score = dimension.get("score")
                    name = str(dimension.get("dimension") or "")
                    if not name or isinstance(score, bool) or not isinstance(score, (int, float)):
                        continue
                    per_dimension.setdefault(name, []).append(float(score))
            for name, values in per_dimension.items():
                dimension_values.setdefault(name, {}).setdefault(side, []).append(sum(values) / len(values))
    mean_delta = sum(deltas) / len(deltas) if deltas else 0.0
    dimension_deltas = {}
    for name, sides in sorted(dimension_values.items()):
        champion_values = sides.get("champion", [])
        challenger_values = sides.get("challenger", [])
        if not champion_values or not challenger_values:
            continue
        champion_score = sum(champion_values) / len(champion_values)
        challenger_score = sum(challenger_values) / len(challenger_values)
        dimension_deltas[name] = {
            "champion": champion_score,
            "challenger": challenger_score,
            "delta": challenger_score - champion_score,
        }
    context_comparable = (
        champion_context_hash is None and challenger_context_hash is None
    ) or (
        champion_context_hash is not None
        and challenger_context_hash is not None
        and champion_context_hash == challenger_context_hash
    )
    case_set_comparable = not champion_only and not challenger_only and not manifest_mismatches
    evidence_blocked = sorted(set(champion_evidence_blocked + challenger_evidence_blocked))
    # A newly passing Case is useful evidence, but it cannot by itself waive
    # the paired minimum-effect gate.  The aggregate delta is the frozen
    # objective for this comparison; requiring it here prevents a candidate
    # from being promoted when one Case improves while the rest materially
    # regress (without crossing a hard gate).
    accepted = bool(common) and not malformed_aggregate_case_ids and not legacy_aggregate_case_ids and case_set_comparable and context_comparable and not regressions and not evidence_blocked and not hard_gate_failures and not insufficient_stability and (
        mean_delta >= minimum_effect
    )
    reasons = []
    if not common:
        reasons.append("no_comparable_cases")
    if not case_set_comparable:
        reasons.append("case_set_mismatch" if champion_only or challenger_only else "attempt_manifest_mismatch")
    if not context_comparable:
        reasons.append("comparison_context_mismatch")
    if malformed_aggregate_case_ids:
        reasons.append("malformed_aggregate")
    if legacy_aggregate_case_ids:
        reasons.append("legacy_aggregate")
    if regressions:
        reasons.append("critical_regression")
    if evidence_blocked:
        reasons.append("insufficient_evidence")
    if hard_gate_failures:
        reasons.append("hard_gate_failure")
    if insufficient_stability:
        reasons.append("insufficient_stability")
    if mean_delta < minimum_effect:
        reasons.append("minimum_effect_not_met")
    result = {
        "api_version": CANDIDATE_COMPARISON_API_VERSION,
        "champion_id": champion_id,
        "challenger_id": challenger_id,
        "comparable_case_ids": common,
        "champion_only_case_ids": champion_only,
        "challenger_only_case_ids": challenger_only,
        "malformed_aggregate_case_ids": malformed_aggregate_case_ids,
        "legacy_aggregate_case_ids": legacy_aggregate_case_ids,
        "case_set_comparable": case_set_comparable,
        "attempt_manifest_mismatch_case_ids": sorted(set(manifest_mismatches)),
        "hard_regression_case_ids": regressions,
        "evidence_blocked_case_ids": evidence_blocked,
        "champion_evidence_blocked_case_ids": champion_evidence_blocked,
        "challenger_evidence_blocked_case_ids": challenger_evidence_blocked,
        "hard_gate_failure_case_ids": hard_gate_failures,
        "insufficient_stability_case_ids": sorted(set(insufficient_stability)),
        "improved_case_ids": improvements,
        "paired_mean_delta": mean_delta,
        "dimension_deltas": dimension_deltas,
        "minimum_effect": minimum_effect,
        "champion_context_hash": champion_context_hash,
        "challenger_context_hash": challenger_context_hash,
        "context_comparable": context_comparable,
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
    required_case_ids: Optional[Sequence[str]] = None,
) -> Mapping[str, Any]:
    all_aggregates = tuple(case_aggregates)
    if required_case_ids is None:
        aggregates = all_aggregates
    else:
        required = {str(item) for item in required_case_ids}
        # Exploratory/model-proposed Cases remain visible in the report, but
        # they do not become a hidden convergence blocker once the trusted
        # authorizable set has independently met its stability gate.  If no
        # trusted ids are supplied, retain the conservative all-Cases rule.
        aggregates = (
            tuple(item for item in all_aggregates if str(item.get("case_id")) in required)
            if required
            else all_aggregates
        )
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
