"""Evidence-based execution-path conformance for Skill runs."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Mapping, Optional, Sequence, Tuple


EXECUTION_PATH_SPEC_API_VERSION = "aceval.execution-path-spec/v1"
TRACE_CONFORMANCE_API_VERSION = "aceval.trace-conformance/v1"
STEP_KINDS = ("required", "recommended", "alternative", "forbidden")


class ExecutionPathError(ValueError):
    pass


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionPathError("%s must be an object" % label)
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ExecutionPathError("%s must be a non-empty trimmed string" % label)
    return value


def _texts(value: Any, label: str) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExecutionPathError("%s must be an array" % label)
    return tuple(_text(item, "%s[]" % label) for item in value)


@dataclass(frozen=True)
class ExecutionStepSpec:
    id: str
    label: str
    kind: str
    match: Mapping[str, Any]
    after: Tuple[str, ...] = ()
    alternative_group: Optional[str] = None

    def __post_init__(self) -> None:
        _text(self.id, "step.id")
        _text(self.label, "step.label")
        if self.kind not in STEP_KINDS:
            raise ExecutionPathError("step.kind must be one of %s" % ", ".join(STEP_KINDS))
        matcher = _mapping(self.match, "step.match")
        allowed = {"event_type", "tool_name", "contains", "fields"}
        unknown = sorted(set(matcher).difference(allowed))
        if unknown:
            raise ExecutionPathError("step.match contains unsupported fields: %s" % ", ".join(unknown))
        if not matcher:
            raise ExecutionPathError("step.match must not be empty")
        if "fields" in matcher:
            _mapping(matcher["fields"], "step.match.fields")
        object.__setattr__(self, "after", tuple(self.after))
        if self.kind == "alternative" and not self.alternative_group:
            raise ExecutionPathError("alternative step requires alternative_group")

    @classmethod
    def from_mapping(cls, value: Any) -> "ExecutionStepSpec":
        item = _mapping(value, "execution step")
        return cls(
            id=item.get("id", ""),
            label=item.get("label", ""),
            kind=item.get("kind", ""),
            match=item.get("match", {}),
            after=_texts(item.get("after", []), "step.after"),
            alternative_group=item.get("alternative_group"),
        )


@dataclass(frozen=True)
class ExecutionPathSpec:
    api_version: str
    steps: Tuple[ExecutionStepSpec, ...]
    trace_completeness_required: bool = True

    def __post_init__(self) -> None:
        if self.api_version != EXECUTION_PATH_SPEC_API_VERSION:
            raise ExecutionPathError("unsupported execution path api_version")
        ids = [step.id for step in self.steps]
        if not ids or len(ids) != len(set(ids)):
            raise ExecutionPathError("execution path steps must have unique ids")
        known = set(ids)
        for step in self.steps:
            unknown = set(step.after).difference(known)
            if unknown:
                raise ExecutionPathError("step %s references unknown predecessors" % step.id)

    @classmethod
    def from_mapping(cls, value: Any) -> "ExecutionPathSpec":
        item = _mapping(value, "execution path")
        raw_steps = item.get("steps")
        if isinstance(raw_steps, (str, bytes)) or not isinstance(raw_steps, Sequence):
            raise ExecutionPathError("execution path steps must be an array")
        return cls(
            api_version=item.get("api_version", ""),
            steps=tuple(ExecutionStepSpec.from_mapping(step) for step in raw_steps),
            trace_completeness_required=bool(item.get("trace_completeness_required", True)),
        )


def _field(event: Mapping[str, Any], path: str) -> Any:
    current: Any = event
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _one_or_many(value: Any) -> Tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _matches(event: Mapping[str, Any], matcher: Mapping[str, Any]) -> bool:
    event_type = event.get("kind") or event.get("type")
    tool_name = event.get("tool") or event.get("name") or _field(event, "payload.name")
    if "event_type" in matcher and event_type not in _one_or_many(matcher["event_type"]):
        return False
    if "tool_name" in matcher and tool_name not in _one_or_many(matcher["tool_name"]):
        return False
    text = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
    if "contains" in matcher and not all(str(item) in text for item in _one_or_many(matcher["contains"])):
        return False
    fields = matcher.get("fields", {})
    if isinstance(fields, Mapping) and any(_field(event, str(path)) != expected for path, expected in fields.items()):
        return False
    return True


def evaluate_trace_conformance(
    spec: ExecutionPathSpec,
    trace: Sequence[Mapping[str, Any]],
    *,
    trace_complete: bool,
) -> Mapping[str, Any]:
    """Evaluate semantic checkpoints, not an overfitted exact tool sequence."""

    if spec.trace_completeness_required and not trace_complete:
        return {
            "api_version": TRACE_CONFORMANCE_API_VERSION,
            "status": "not_evaluable",
            "reason": "trace is incomplete; execution-path evidence cannot be trusted",
            "skill_patch_authorized": False,
            "coverage": None,
            "steps": [],
            "violations": [],
        }
    rows = []
    positions = {}
    for step in spec.steps:
        evidence = [index for index, event in enumerate(trace) if _matches(event, step.match)]
        if evidence:
            positions[step.id] = evidence[0]
        rows.append({
            "id": step.id,
            "label": step.label,
            "kind": step.kind,
            "observed": bool(evidence),
            "evidence_event_indexes": evidence,
            "alternative_group": step.alternative_group,
        })
    violations = []
    for step in spec.steps:
        observed = step.id in positions
        if step.kind == "required" and not observed:
            violations.append({"type": "missing_required", "step_id": step.id})
        if step.kind == "forbidden" and observed:
            violations.append({"type": "forbidden_observed", "step_id": step.id, "event_index": positions[step.id]})
        if observed:
            for predecessor in step.after:
                if predecessor in positions and positions[predecessor] > positions[step.id]:
                    violations.append({"type": "ordering", "step_id": step.id, "after": predecessor})
                elif predecessor not in positions:
                    violations.append({"type": "missing_predecessor", "step_id": step.id, "after": predecessor})
    groups = {}
    for step in spec.steps:
        if step.kind == "alternative":
            groups.setdefault(step.alternative_group, []).append(step.id)
    for group, step_ids in groups.items():
        if not any(step_id in positions for step_id in step_ids):
            violations.append({"type": "missing_alternative", "group": group, "step_ids": step_ids})
    required_units = [step.id for step in spec.steps if step.kind == "required"] + ["group:%s" % group for group in groups]
    satisfied = sum(step_id in positions for step_id in required_units if not step_id.startswith("group:"))
    satisfied += sum(any(step_id in positions for step_id in ids) for ids in groups.values())
    coverage = satisfied / len(required_units) if required_units else 1.0
    return {
        "api_version": TRACE_CONFORMANCE_API_VERSION,
        "status": "fail" if violations else "pass",
        "reason": None,
        "skill_patch_authorized": True,
        "coverage": coverage,
        "steps": rows,
        "violations": violations,
    }


__all__ = ["EXECUTION_PATH_SPEC_API_VERSION", "TRACE_CONFORMANCE_API_VERSION", "ExecutionPathError", "ExecutionPathSpec", "ExecutionStepSpec", "evaluate_trace_conformance"]
