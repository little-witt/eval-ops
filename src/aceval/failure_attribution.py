"""Evidence-backed failure attribution and Skill intervention authorization.

This module deliberately separates an observed failure from a causal claim.
Deterministic rules classify runtime/evaluator/tool signals and decide whether
the current evidence may be used to change a Skill.  Language models may later
explain these records, but they are not part of the authorization path.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .contracts import GradeResult, GradeStatus, RunObservation, TraceEvent, as_primitive


FAILURE_CARD_API_VERSION = "aceval.failure-card/v1"
DIAGNOSTIC_REPORT_API_VERSION = "aceval.diagnostic-report/v1"
ATTRIBUTION_RULESET_VERSION = "aceval.failure-rules/v1"


class PatchDecision(str, Enum):
    NONE = "none"
    ALLOW_SKILL_INTERVENTION = "allow_skill_intervention"
    DENY_SKILL_INTERVENTION = "deny_skill_intervention"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


class EvidenceStrength(str, Enum):
    DIRECT = "direct"
    CORROBORATED = "corroborated"
    INFERRED = "inferred"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class DiagnosticSignal:
    code: str
    producer: str
    phase: str
    message: str
    component: str
    evidence_ref: str
    data: Mapping[str, Any] = field(default_factory=dict)
    expected: bool = False

    def to_dict(self) -> Mapping[str, Any]:
        return as_primitive(self)


@dataclass(frozen=True)
class FailureCard:
    failure_id: str
    scenario_id: str
    symptom: str
    category: str
    observed_component: str
    remediation_surface: str
    confidence: EvidenceStrength
    evidence: Tuple[Mapping[str, Any], ...]
    alternative_hypotheses: Tuple[str, ...]
    patch_decision: PatchDecision
    recommended_actions: Tuple[str, ...]
    reason_code: str
    additional_probe: Optional[str] = None
    expected_fault: bool = False
    api_version: str = FAILURE_CARD_API_VERSION

    @property
    def skill_patch_authorized(self) -> bool:
        return self.patch_decision == PatchDecision.ALLOW_SKILL_INTERVENTION

    def to_dict(self) -> Mapping[str, Any]:
        value = dict(as_primitive(self))
        value["skill_patch_authorized"] = self.skill_patch_authorized
        return value


@dataclass(frozen=True)
class DiagnosticReport:
    scenario_id: str
    failure_cards: Tuple[FailureCard, ...]
    patch_decision: PatchDecision
    evaluable: bool
    blocked_reasons: Tuple[str, ...] = ()
    ruleset_version: str = ATTRIBUTION_RULESET_VERSION
    api_version: str = DIAGNOSTIC_REPORT_API_VERSION

    @property
    def skill_patch_allowed(self) -> bool:
        return self.patch_decision == PatchDecision.ALLOW_SKILL_INTERVENTION

    @property
    def eligible_skill_failures(self) -> Tuple[FailureCard, ...]:
        return tuple(card for card in self.failure_cards if card.skill_patch_authorized)

    def to_dict(self) -> Mapping[str, Any]:
        value = dict(as_primitive(self))
        value["skill_patch_allowed"] = self.skill_patch_allowed
        value["failure_cards"] = [card.to_dict() for card in self.failure_cards]
        value["eligible_skill_failures"] = [
            card.failure_id for card in self.eligible_skill_failures
        ]
        return value


_ERROR_RULES = (
    (re.compile(r"collect_error|cleanup_error", re.I), "driver.failure", "driver", "driver"),
    (re.compile(r"fixture|case-input|input fixture", re.I), "fixture.invalid", "fixture", "evalpack"),
    (re.compile(r"grader_error", re.I), "grader.error", "grader", "evalpack"),
    (re.compile(r"oracle|schema.*invalid", re.I), "oracle.invalid", "oracle", "evalpack"),
    (
        re.compile(
            r"runtime_exception|runtime_error|spawn_error|model bridge|configuration_error|"
            r"max_steps|max_tool_calls|wall-time budget|\btimeout\b|timed out",
            re.I,
        ),
        "runtime.failure",
        "runtime",
        "runtime_profile",
    ),
    (re.compile(r"lifecycle_error", re.I), "driver.lifecycle", "driver", "runtime"),
)

_TOOL_STRING_RULES = (
    (re.compile(r"command not found|not found.*command|\benoent\b", re.I), "cli.binary_not_found", "cli", "runtime_profile"),
    (re.compile(r"permission denied|\beacces\b|read-only", re.I), "permission.denied", "environment", "runtime_profile"),
    (re.compile(r"unauthorized|invalid credential|token expired|\b401\b", re.I), "authentication.failed", "environment", "runtime_profile"),
    (re.compile(r"forbidden|\b403\b", re.I), "permission.denied", "environment", "runtime_profile"),
    (re.compile(r"dns|connection refused|network|rate limit|\b429\b", re.I), "network.failure", "environment", "runtime_profile"),
    (re.compile(r"timed out|timeout", re.I), "tool.timeout", "tool", "runtime_profile"),
    (re.compile(r"unknown tool", re.I), "agent.unknown_tool", "agent", "skill_instruction"),
    (re.compile(r"argument|must be a|string|required field|invalid option", re.I), "agent.bad_argument", "agent", "skill_instruction"),
)


def _event_payload(event: Any) -> Mapping[str, Any]:
    if isinstance(event, TraceEvent):
        return event.payload
    if isinstance(event, Mapping):
        payload = event.get("payload", {})
        if isinstance(payload, Mapping):
            merged = dict(payload)
            for key, value in event.items():
                if key not in {"payload", "kind", "type", "seq", "name", "tool"}:
                    merged.setdefault(str(key), value)
            return merged
    return {}


def _event_kind(event: Any) -> str:
    if isinstance(event, TraceEvent):
        return event.kind
    if isinstance(event, Mapping):
        return str(event.get("kind") or event.get("type") or "")
    return ""


def _event_tool(event: Any, payload: Mapping[str, Any]) -> str:
    value = getattr(event, "tool", None)
    if value is None and isinstance(event, Mapping):
        value = event.get("tool") or event.get("name")
    if value is None:
        value = payload.get("tool") or payload.get("name")
    return str(value or "")


def _event_seq(event: Any, fallback: int) -> int:
    value = getattr(event, "seq", None)
    if value is None and isinstance(event, Mapping):
        value = event.get("seq")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else fallback


def _normalize_expected_signals(value: Any) -> Tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        value = (value,)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _is_expected(code: str, tool: str, expected: Sequence[Mapping[str, Any]]) -> bool:
    for item in expected:
        expected_code = str(item.get("code", ""))
        expected_tool = str(item.get("tool", ""))
        if expected_code and expected_code != code:
            continue
        if expected_tool and expected_tool != tool:
            continue
        if expected_code or expected_tool:
            return True
    return False


def _tool_signal(event: Any, index: int, expected: Sequence[Mapping[str, Any]]) -> Optional[DiagnosticSignal]:
    if _event_kind(event) != "tool_result":
        return None
    payload = _event_payload(event)
    tool = _event_tool(event, payload)
    ok = payload.get("ok")
    is_error = payload.get("is_error")
    exit_code = payload.get("exit_code")
    status = payload.get("status")
    error_type = str(payload.get("error_type") or "").strip().casefold()
    error = getattr(event, "error", None) or payload.get("error") or payload.get("stderr_excerpt")
    timed_out = payload.get("timed_out") is True
    http_status = payload.get("http_status")
    if (
        (ok is True or is_error is False)
        and not timed_out
        and error is None
        and exit_code in (None, 0)
    ):
        return None

    code = "tool.failure"
    component = "tool"
    remediation = "runtime_profile"
    message = str(error or error_type or status or "tool execution failed")
    if error_type in {"command_not_found", "binary_not_found", "enoent"} or exit_code == 127:
        code, component = "cli.binary_not_found", "cli"
    elif error_type in {"permission_denied", "eacces"} or exit_code == 126 or http_status == 403:
        code, component = "permission.denied", "environment"
    elif error_type in {"authentication", "unauthorized"} or http_status == 401:
        code, component = "authentication.failed", "environment"
    elif error_type in {"network", "dns", "connection"} or http_status == 429 or (
        isinstance(http_status, int) and 500 <= http_status <= 599
    ):
        code, component = "network.failure", "environment"
    elif timed_out or error_type == "timeout":
        code, component = "tool.timeout", "tool"
    else:
        for pattern, candidate, candidate_component, candidate_remediation in _TOOL_STRING_RULES:
            if pattern.search(message):
                code = candidate
                component = candidate_component
                remediation = candidate_remediation
                break
        if code == "tool.failure" and isinstance(exit_code, int) and exit_code != 0:
            code, component = "cli.non_zero_exit", "cli"

    expected_fault = _is_expected(code, tool, expected)
    return DiagnosticSignal(
        code=code,
        producer="runtime",
        phase="tool",
        message=message,
        component=component,
        evidence_ref="trace:%s" % _event_seq(event, index),
        data={
            "tool": tool,
            "exit_code": exit_code,
            "http_status": http_status,
            "error_type": error_type or None,
            "is_error": is_error if isinstance(is_error, bool) else None,
            "remediation_surface": remediation,
        },
        expected=expected_fault,
    )


def _failure_id(scenario_id: str, reason_code: str, evidence: Sequence[Mapping[str, Any]]) -> str:
    primitive_evidence = as_primitive(tuple(evidence))

    def identity_default(value: Any) -> Mapping[str, Any]:
        if isinstance(value, (bytes, bytearray, memoryview)):
            content = bytes(value)
            return {
                "$type": "bytes",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        raise TypeError("unsupported failure evidence type: %s" % type(value).__name__)

    payload = json.dumps(
        {
            "scenario_id": scenario_id,
            "reason_code": reason_code,
            "evidence": primitive_evidence,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=identity_default,
    ).encode("utf-8")
    return "f-" + hashlib.sha256(payload).hexdigest()[:16]


def _card(
    scenario_id: str,
    *,
    symptom: str,
    category: str,
    component: str,
    remediation: str,
    confidence: EvidenceStrength,
    evidence: Sequence[Mapping[str, Any]],
    decision: PatchDecision,
    reason_code: str,
    actions: Sequence[str],
    alternatives: Sequence[str] = (),
    additional_probe: Optional[str] = None,
    expected_fault: bool = False,
) -> FailureCard:
    frozen_evidence = tuple(dict(item) for item in evidence)
    return FailureCard(
        failure_id=_failure_id(scenario_id, reason_code, frozen_evidence),
        scenario_id=scenario_id,
        symptom=symptom,
        category=category,
        observed_component=component,
        remediation_surface=remediation,
        confidence=confidence,
        evidence=frozen_evidence,
        alternative_hypotheses=tuple(alternatives),
        patch_decision=decision,
        recommended_actions=tuple(actions),
        reason_code=reason_code,
        additional_probe=additional_probe,
        expected_fault=expected_fault,
    )


def _aggregate_decision(cards: Sequence[FailureCard]) -> PatchDecision:
    decisions = {card.patch_decision for card in cards}
    if PatchDecision.DENY_SKILL_INTERVENTION in decisions:
        return PatchDecision.DENY_SKILL_INTERVENTION
    if PatchDecision.NEEDS_MORE_EVIDENCE in decisions:
        return PatchDecision.NEEDS_MORE_EVIDENCE
    if PatchDecision.ALLOW_SKILL_INTERVENTION in decisions:
        return PatchDecision.ALLOW_SKILL_INTERVENTION
    return PatchDecision.NONE


class FailureAttributor:
    """Deterministic D20 classifier with a conservative intervention gate."""

    def attribute(
        self,
        scenario_id: str,
        observation: Optional[RunObservation] = None,
        grades: Sequence[GradeResult] = (),
        error: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> DiagnosticReport:
        scenario_id = str(scenario_id or "scenario")
        metadata = dict(metadata or {})
        attribution = metadata.get("attribution", {})
        if not isinstance(attribution, Mapping):
            attribution = {}
        expected = _normalize_expected_signals(
            attribution.get("expected_signals", metadata.get("expected_signals"))
        )
        cards = []

        missing_fields = metadata.get("missing_observation_fields", ())
        if isinstance(missing_fields, str):
            missing_fields = (missing_fields,)
        if isinstance(missing_fields, Sequence) and not isinstance(
            missing_fields, (str, bytes)
        ):
            missing_fields = tuple(
                str(item) for item in missing_fields if str(item)
            )
        else:
            missing_fields = ()
        if missing_fields:
            cards.append(
                _card(
                    scenario_id,
                    symptom="missing observation fields: %s"
                    % ", ".join(sorted(missing_fields)),
                    category="evidence_gap",
                    component="observation",
                    remediation="runtime_profile",
                    confidence=EvidenceStrength.DIRECT,
                    evidence=(
                        {
                            "kind": "observation_completeness",
                            "ref": "observation:completeness",
                            "missing": list(sorted(missing_fields)),
                        },
                    ),
                    decision=PatchDecision.DENY_SKILL_INTERVENTION,
                    reason_code="evidence.missing",
                    actions=(
                        "Collect the missing observation channels before changing the Skill.",
                    ),
                )
            )

        text_error = str(error or (observation.error if observation is not None else "") or "")
        if text_error:
            matched = False
            for pattern, reason, component, remediation in _ERROR_RULES:
                if pattern.search(text_error):
                    cards.append(
                        _card(
                            scenario_id,
                            symptom=text_error,
                            category="execution" if component in {"runtime", "driver"} else "evaluator",
                            component=component,
                            remediation=remediation,
                            confidence=EvidenceStrength.DIRECT,
                            evidence=({"kind": "scenario_error", "ref": "scenario:error", "message": text_error},),
                            decision=PatchDecision.DENY_SKILL_INTERVENTION,
                            reason_code=reason,
                            actions=("Repair the failing %s component and rerun the same Case." % remediation,),
                        )
                    )
                    matched = True
                    break
            if not matched:
                cards.append(
                    _card(
                        scenario_id,
                        symptom=text_error,
                        category="execution",
                        component="unknown",
                        remediation="manual",
                        confidence=EvidenceStrength.INSUFFICIENT,
                        evidence=({"kind": "scenario_error", "ref": "scenario:error", "message": text_error},),
                        decision=PatchDecision.NEEDS_MORE_EVIDENCE,
                        reason_code="execution.unknown",
                        actions=("Collect structured runtime/tool diagnostics before changing the Skill.",),
                        additional_probe="repeat_same_configuration",
                    )
                )

        signals = []
        if observation is not None:
            for index, event in enumerate(observation.trace):
                signal = _tool_signal(event, index, expected)
                if signal is not None:
                    signals.append(signal)
        for signal in signals:
            if signal.expected:
                continue
            reason = signal.code
            if reason in {"agent.unknown_tool", "agent.bad_argument"}:
                decision = PatchDecision.NEEDS_MORE_EVIDENCE
                confidence = EvidenceStrength.INFERRED
                actions = (
                    "Compare the tool call with the source-grounded Skill requirement and repeat the Case.",
                )
                probe = "repeat_and_compare_skill_instruction"
            else:
                decision = PatchDecision.DENY_SKILL_INTERVENTION
                confidence = EvidenceStrength.DIRECT
                actions = ("Repair the external tool/runtime dependency, then rerun the Case.",)
                probe = None
            remediation = str(signal.data.get("remediation_surface") or "runtime_profile")
            cards.append(
                _card(
                    scenario_id,
                    symptom=signal.message,
                    category="tool_or_dependency",
                    component=signal.component,
                    remediation=remediation,
                    confidence=confidence,
                    evidence=(
                        {
                            "kind": "diagnostic_signal",
                            "ref": signal.evidence_ref,
                            "code": signal.code,
                            "data": dict(signal.data),
                        },
                    ),
                    decision=decision,
                    reason_code=reason,
                    actions=actions,
                    alternatives=("The Skill may have instructed an invalid command or argument.",)
                    if reason.startswith("cli.")
                    else (),
                    additional_probe=probe,
                )
            )

        evaluator_blocked = False
        hard_failures = []
        for grade in grades:
            if grade.status == GradeStatus.ERROR:
                evaluator_blocked = True
                message = grade.message or "grader returned ERROR"
                reason = "oracle.invalid" if re.search(r"oracle|schema", message, re.I) else "grader.error"
                component = "oracle" if reason == "oracle.invalid" else "grader"
                cards.append(
                    _card(
                        scenario_id,
                        symptom=message,
                        category="evaluator",
                        component=component,
                        remediation="evalpack",
                        confidence=EvidenceStrength.DIRECT,
                        evidence=({"kind": "grade", "ref": "grade:%s" % grade.grader_id, "status": grade.status.value},),
                        decision=PatchDecision.DENY_SKILL_INTERVENTION,
                        reason_code=reason,
                        actions=("Repair and version the EvalPack evaluator before rerunning baseline.",),
                    )
                )
            elif grade.status == GradeStatus.NOT_EVALUABLE:
                evaluator_blocked = True
                missing = tuple(grade.missing)
                cards.append(
                    _card(
                        scenario_id,
                        symptom=grade.message or "required evaluation evidence was not observed",
                        category="evidence_gap",
                        component="observation",
                        remediation="runtime_profile",
                        confidence=EvidenceStrength.DIRECT,
                        evidence=({"kind": "grade", "ref": "grade:%s" % grade.grader_id, "missing": list(missing)},),
                        decision=PatchDecision.DENY_SKILL_INTERVENTION,
                        reason_code="evidence.missing",
                        actions=("Collect the missing observation channels before changing the Skill.",),
                    )
                )
            elif grade.hard and grade.status == GradeStatus.FAIL:
                hard_failures.append(grade)

        external_block = any(
            card.patch_decision == PatchDecision.DENY_SKILL_INTERVENTION
            for card in cards
        )
        expected_faults = tuple(signal for signal in signals if signal.expected)
        if hard_failures and not external_block and not evaluator_blocked:
            recovery = bool(expected_faults)
            for grade in hard_failures:
                grade_evidence = []
                for item in grade.evidence:
                    grade_evidence.append(dict(item) if isinstance(item, Mapping) else {"value": str(item)})
                grade_evidence.append(
                    {
                        "kind": "grade",
                        "ref": "grade:%s" % grade.grader_id,
                        "status": grade.status.value,
                        "message": grade.message,
                    }
                )
                for signal in expected_faults:
                    grade_evidence.append(
                        {
                            "kind": "expected_fault",
                            "ref": signal.evidence_ref,
                            "code": signal.code,
                        }
                    )
                cards.append(
                    _card(
                        scenario_id,
                        symptom=grade.message or "hard quality gate failed",
                        category="recovery_failure" if recovery else "quality_failure",
                        component="agent_behavior",
                        remediation="skill_instruction",
                        confidence=EvidenceStrength.INFERRED,
                        evidence=grade_evidence,
                        decision=PatchDecision.ALLOW_SKILL_INTERVENTION,
                        reason_code="agent.recovery_miss" if recovery else "agent.quality_failure",
                        actions=(
                            "Generate a constrained Skill candidate and verify it on dev/validation gates.",
                        ),
                        alternatives=(
                            "The underlying model may be flaky; repeat the baseline before making a strong causal claim.",
                        ),
                        additional_probe="repeat_same_configuration",
                        expected_fault=recovery,
                    )
                )

        decision = _aggregate_decision(cards)
        blocked = tuple(
            dict.fromkeys(
                card.reason_code
                for card in cards
                if card.patch_decision
                in (PatchDecision.DENY_SKILL_INTERVENTION, PatchDecision.NEEDS_MORE_EVIDENCE)
            )
        )
        evaluable = not any(
            card.category in {"evaluator", "evidence_gap"}
            or card.observed_component in {"runtime", "driver", "fixture"}
            for card in cards
            if not card.expected_fault
        )
        return DiagnosticReport(
            scenario_id=scenario_id,
            failure_cards=tuple(cards),
            patch_decision=decision,
            evaluable=evaluable,
            blocked_reasons=blocked,
        )

    def attribute_scenario(
        self,
        evaluation: Any,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> DiagnosticReport:
        return self.attribute(
            scenario_id=str(getattr(evaluation, "scenario_id", "scenario")),
            observation=getattr(evaluation, "observation", None),
            grades=tuple(getattr(evaluation, "grades", ()) or ()),
            error=getattr(evaluation, "error", None),
            metadata=metadata,
        )


def attribute_failure(
    scenario_id: str,
    observation: Optional[RunObservation] = None,
    grades: Sequence[GradeResult] = (),
    error: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> DiagnosticReport:
    return FailureAttributor().attribute(
        scenario_id, observation=observation, grades=grades, error=error, metadata=metadata
    )


__all__ = [
    "ATTRIBUTION_RULESET_VERSION",
    "DIAGNOSTIC_REPORT_API_VERSION",
    "FAILURE_CARD_API_VERSION",
    "DiagnosticReport",
    "DiagnosticSignal",
    "EvidenceStrength",
    "FailureAttributor",
    "FailureCard",
    "PatchDecision",
    "attribute_failure",
]
