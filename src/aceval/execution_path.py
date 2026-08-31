"""Evidence-based execution-path conformance for Skill runs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union


EXECUTION_PATH_SPEC_API_VERSION = "aceval.execution-path-spec/v1"
TRACE_CONFORMANCE_API_VERSION = "aceval.trace-conformance/v1"
# V3 path-analysis documents are design/read-model contracts.  Execution
# remains deliberately backed by the strict v1 evaluator until a future
# version adds first-class conditional edges and state semantics.
PATH_ANALYSIS_API_VERSION = "aceval.path-analysis/v3"
EXECUTION_PATH_SPEC_V3_API_VERSION = "aceval.execution-path-spec/v3"
STEP_KINDS = ("required", "recommended", "alternative", "forbidden")
_V3_API_VERSIONS = frozenset((PATH_ANALYSIS_API_VERSION, EXECUTION_PATH_SPEC_V3_API_VERSION))


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


def _alias_value(
    item: Mapping[str, Any], canonical: str, aliases: Sequence[str], label: str
) -> Any:
    """Read a canonical field and its V3 aliases without accepting conflicts."""

    values = []
    for key in (canonical,) + tuple(aliases):
        if key in item and item[key] is not None:
            values.append((key, item[key]))
    if not values:
        return None
    first_key, first_value = values[0]
    for key, value in values[1:]:
        if value != first_value:
            raise ExecutionPathError(
                "%s has conflicting %s and %s values" % (label, first_key, key)
            )
    return first_value


def _edge_endpoints(edge: Any, index: int) -> Tuple[str, str]:
    value = _mapping(edge, "execution path edge[%d]" % index)
    source = _alias_value(value, "source", ("from",), "execution path edge[%d]" % index)
    target = _alias_value(value, "target", ("to",), "execution path edge[%d]" % index)
    if source is None or target is None:
        raise ExecutionPathError(
            "execution path edge[%d] must define source/from and target/to" % index
        )
    return _text(source, "execution path edge[%d].source" % index), _text(
        target, "execution path edge[%d].target" % index
    )


def _edge_is_unconditional_order(edge: Mapping[str, Any]) -> bool:
    """Return whether a V3 edge can be represented faithfully by v1 ``after``.

    Failure, retry, fallback, alternative, and guarded edges carry branch
    semantics that the v1 evaluator cannot express.  Treating them as an
    unconditional predecessor creates false path failures, so those edges stay
    in the V3 read model and are deliberately omitted from v1 evaluation.
    """

    for key in ("when", "condition", "guard", "predicate"):
        if key in edge and edge[key] not in (None, "", True):
            return False
    relation = edge.get("kind") or edge.get("relation") or edge.get("type")
    if relation is None:
        return True
    return str(relation).strip().casefold() in {
        "after",
        "next",
        "order",
        "ordered",
        "sequence",
        "sequential",
        "unconditional",
    }


def _edge_predecessors(
    value: Mapping[str, Any], known_node_ids: Sequence[str]
) -> Mapping[str, Tuple[str, ...]]:
    """Convert only unconditional V3 ordering edges to v1 ``after``.

    All endpoints are still validated, including endpoints of conditional
    edges that are not executable by v1.  This keeps malformed Path IR from
    silently becoming a different path during compatibility normalization.
    """

    raw_edges = value.get("edges")
    if raw_edges is None:
        return {}
    if isinstance(raw_edges, (str, bytes)) or not isinstance(raw_edges, Sequence):
        raise ExecutionPathError("execution path edges must be an array")
    known = set(known_node_ids)
    result: Dict[str, List[str]] = {}
    for index, raw_edge in enumerate(raw_edges):
        edge = _mapping(raw_edge, "execution path edge[%d]" % index)
        source, target = _edge_endpoints(raw_edge, index)
        unknown = sorted({source, target}.difference(known))
        if unknown:
            raise ExecutionPathError(
                "execution path edge[%d] references unknown nodes: %s"
                % (index, ", ".join(unknown))
            )
        if not _edge_is_unconditional_order(edge):
            continue
        result.setdefault(target, [])
        if source not in result[target]:
            result[target].append(source)
    return {key: tuple(items) for key, items in result.items()}


def _normalize_step_mapping(
    value: Any,
    index: int,
    *,
    v3: bool = False,
    edge_after: Sequence[str] = (),
) -> Mapping[str, Any]:
    """Make a detached v1-shaped step mapping.

    For legacy ``steps`` input this is intentionally a no-op copy so existing
    validation/error behavior is preserved.  V3 ``nodes`` input accepts only
    aliases that can be represented by the v1 evaluator; richer fields remain
    ignored by the evaluator and available to the read model.
    """

    raw = _mapping(value, "execution step[%d]" % index)
    if not v3:
        return dict(raw)
    item: Dict[str, Any] = dict(raw)
    step_label = "execution step[%d]" % index
    step_id = _alias_value(item, "id", ("step_id",), step_label)
    if step_id is not None:
        item["id"] = step_id
    # ``action`` is the textual V3 spelling used by Path IR.  Keep v1's
    # required label validation if neither spelling is supplied.
    label = _alias_value(item, "label", ("action",), step_label)
    if label is not None:
        item["label"] = label
    requiredness = _alias_value(item, "kind", ("requiredness",), step_label)
    if requiredness is not None:
        item["kind"] = requiredness
    alternative_group = _alias_value(
        item, "alternative_group", ("group",), step_label
    )
    if alternative_group is not None:
        item["alternative_group"] = alternative_group

    after_values: List[str] = []
    for field_name in ("after", "predecessors"):
        if field_name not in item or item[field_name] is None:
            continue
        for predecessor in _texts(item[field_name], "%s.%s" % (step_label, field_name)):
            if predecessor not in after_values:
                after_values.append(predecessor)
    for predecessor in edge_after:
        predecessor = _text(predecessor, "%s.after[]" % step_label)
        if predecessor not in after_values:
            after_values.append(predecessor)
    if after_values or any(field in item for field in ("after", "predecessors")) or edge_after:
        item["after"] = after_values

    # V3 Path IR often describes observable tool/command facts instead of a
    # v1 matcher.  Derive a narrow matcher only when an explicit matcher is
    # absent; an entirely unobservable node still fails v1 validation.
    matcher = item.get("match")
    if matcher is None or matcher == {}:
        derived: Dict[str, Any] = {}
        tool = item.get("tool")
        if tool is not None:
            derived["tool_name"] = tool
        observables = item.get("observables")
        if isinstance(observables, str):
            observables = [observables]
        if isinstance(observables, Sequence) and not isinstance(observables, (str, bytes)) and observables:
            derived["contains"] = [str(observable) for observable in observables]
        command = item.get("command")
        if isinstance(command, Mapping):
            argv = command.get("argv")
            if isinstance(argv, Sequence) and not isinstance(argv, (str, bytes)) and argv:
                derived.setdefault("command_contains", [str(argument) for argument in argv])
        if derived:
            item["match"] = derived
    return item


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
        allowed = {
            "event_type",
            "tool_name",
            "contains",
            "command_contains",
            "fields",
        }
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
        # Accept the small V3 alias set when a step is parsed directly.  The
        # same normalized shape is used by ``ExecutionPathSpec.from_mapping``.
        normalized = _normalize_step_mapping(
            item,
            0,
            v3=any(
                key in item
                for key in (
                    "step_id",
                    "requiredness",
                    "predecessors",
                    "action",
                    "group",
                    "tool",
                    "observables",
                    "command",
                )
            ),
        )
        return cls(
            id=normalized.get("id", ""),
            label=normalized.get("label", ""),
            kind=normalized.get("kind", ""),
            match=normalized.get("match", {}),
            after=_texts(normalized.get("after", []), "step.after"),
            alternative_group=normalized.get("alternative_group"),
        )


@dataclass(frozen=True)
class ExecutionPathSpec:
    api_version: str
    steps: Tuple[ExecutionStepSpec, ...]
    trace_completeness_required: bool = True
    read_only_compatibility: bool = False

    def __post_init__(self) -> None:
        if self.api_version != EXECUTION_PATH_SPEC_API_VERSION:
            raise ExecutionPathError("unsupported execution path api_version")
        ids = [step.id for step in self.steps]
        if not ids or len(ids) != len(set(ids)):
            raise ExecutionPathError("execution path steps must have unique ids")
        known = set(ids)
        for step in self.steps:
            if step.id in step.after:
                raise ExecutionPathError(
                    "step %s cannot reference itself as a predecessor" % step.id
                )
            unknown = set(step.after).difference(known)
            if unknown:
                raise ExecutionPathError("step %s references unknown predecessors" % step.id)

    @classmethod
    def from_mapping(cls, value: Any) -> "ExecutionPathSpec":
        item = _mapping(value, "execution path")
        api_version = item.get("api_version", "")
        v3 = api_version in _V3_API_VERSIONS or "nodes" in item
        # ``steps`` remains the canonical v1 spelling.  A document containing
        # both forms is read without mutating either one; v1 ``steps`` wins so
        # old producers retain their exact behavior.
        raw_steps = item.get("steps") if "steps" in item else item.get("nodes")
        if isinstance(raw_steps, (str, bytes)) or not isinstance(raw_steps, Sequence):
            raise ExecutionPathError("execution path steps must be an array")
        node_ids = tuple(
            str(
                _alias_value(
                    step if isinstance(step, Mapping) else {},
                    "id",
                    ("step_id",),
                    "execution step[%d]" % index,
                )
                or ""
            )
            for index, step in enumerate(raw_steps)
        )
        edge_predecessors = (
            _edge_predecessors(item, node_ids) if v3 or "edges" in item else {}
        )
        normalized_steps = tuple(
            _normalize_step_mapping(
                step,
                index,
                v3=v3,
                edge_after=edge_predecessors.get(
                    str(
                        _alias_value(
                            step if isinstance(step, Mapping) else {},
                            "id",
                            ("step_id",),
                            "execution step[%d]" % index,
                        )
                        or ""
                    ),
                    (),
                ),
            )
            for index, step in enumerate(raw_steps)
        )
        return cls(
            # Normalize recognized V3 design documents to the strict v1
            # evaluator contract.  Unknown versions still fail in __post_init__.
            api_version=(EXECUTION_PATH_SPEC_API_VERSION if api_version in _V3_API_VERSIONS else api_version),
            steps=tuple(ExecutionStepSpec.from_mapping(step) for step in normalized_steps),
            trace_completeness_required=bool(item.get("trace_completeness_required", True)),
            # Path IR v3 carries branch, failure, retry, state and side-effect
            # semantics that the v1 matcher cannot grade faithfully.  Parsing
            # it is useful for validation and UI compatibility, but execution
            # must remain not-evaluable until a native V3 evaluator exists.
            read_only_compatibility=v3,
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


def _command_text(event: Mapping[str, Any]) -> Optional[str]:
    """Return only executable command/argv fields from a tool-call event.

    Searching the whole event for a command-shaped string is unsafe: an
    instruction such as "do not run git push" is evidence about policy, not
    evidence that the command was executed.
    """

    event_type = str(event.get("kind") or event.get("type") or "")
    if event_type not in (
        "tool_call", "tool", "tool_start", "agent.tool_use",
        "assistant.tool_use", "function_call",
    ):
        return None
    values = []
    for path in (
        "command",
        "argv",
        # CATX Agent traces keep the executable payload directly under
        # ``input`` (for example ``{"type":"agent.tool_use",
        # "input":{"command":"node ..."}}``).  The previous matcher only
        # looked through provider-specific wrappers, so a real command was
        # silently reported as missing.
        "input.command",
        "input.argv",
        "payload.command",
        "payload.argv",
        "payload.input.command",
        "payload.input.argv",
        "arguments.command",
        "arguments.argv",
    ):
        value = _field(event, path)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            values.append(" ".join(str(item) for item in value))
    return "\n".join(values) if values else None


def _matches(event: Mapping[str, Any], matcher: Mapping[str, Any]) -> bool:
    event_type = event.get("kind") or event.get("type")
    tool_name = event.get("tool") or event.get("name") or _field(event, "payload.name")
    if "event_type" in matcher:
        expected_types = _one_or_many(matcher["event_type"])
        # CATX/Agent traces use ``agent.tool_use`` for the same semantic
        # action represented as ``tool_call`` in the execution-path IR. Keep
        # the IR provider-neutral while still requiring an actual tool event.
        tool_event_aliases = {"tool_call", "tool", "tool_start", "agent.tool_use", "assistant.tool_use", "function_call"}
        output_event_aliases = {"agent.output", "agent.message", "assistant.message", "assistant.output", "message"}
        type_matches = event_type in expected_types
        if not type_matches and "tool_call" in expected_types and event_type in tool_event_aliases:
            type_matches = True
        # Output checkpoints are authored as ``agent.output`` in generated
        # paths, while CATX receipts expose the same final text as an
        # ``agent.message`` event.  Treat these provider-neutral aliases as
        # equivalent, but never treat a tool result as an output event.
        if not type_matches and "agent.output" in expected_types and event_type in output_event_aliases:
            type_matches = True
        if not type_matches:
            return False
    if "tool_name" in matcher and tool_name not in _one_or_many(matcher["tool_name"]):
        return False
    text = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
    if "contains" in matcher and not all(str(item) in text for item in _one_or_many(matcher["contains"])):
        return False
    if "command_contains" in matcher:
        command_text = _command_text(event)
        if command_text is None or not all(
            str(item) in command_text
            for item in _one_or_many(matcher["command_contains"])
        ):
            return False
    fields = matcher.get("fields", {})
    if isinstance(fields, Mapping) and any(_field(event, str(path)) != expected for path, expected in fields.items()):
        return False
    return True


ExecutionPathInput = Union["ExecutionPathSpec", Mapping[str, Any]]


def resolve_execution_path(
    value: Optional[ExecutionPathInput], case_id: Optional[str] = None
) -> Optional["ExecutionPathSpec"]:
    """Resolve one path from a legacy spec or a per-case path catalog.

    This helper is intentionally non-mutating.  A direct v1/V3 path mapping
    is parsed as one path; a mapping with ``paths``/``execution_paths`` (or
    case-id keys) is treated as a catalog and the requested case is selected.
    Unknown catalog cases return ``None`` so callers can preserve optional
    path behavior and report the missing binding explicitly.
    """

    if value is None:
        return None
    if isinstance(value, ExecutionPathSpec):
        return value
    if not isinstance(value, Mapping):
        raise ExecutionPathError("execution path input must be an object")

    # A direct path has its own node collection. Parse it before catalog
    # wrappers so a future path document may safely carry auxiliary metadata
    # named ``paths`` without being mistaken for a per-case catalog.
    if "steps" in value or "nodes" in value:
        return ExecutionPathSpec.from_mapping(value)

    catalog = value.get("paths")
    if not isinstance(catalog, Mapping):
        catalog = value.get("execution_paths")
    if isinstance(catalog, Mapping):
        if case_id is None:
            return None
        candidate = catalog.get(case_id)
        if candidate is None:
            return None
        if not isinstance(candidate, Mapping):
            raise ExecutionPathError("path for case %s must be an object" % case_id)
        return ExecutionPathSpec.from_mapping(candidate)

    # A bare mapping is retained as a compact per-case catalog for callers
    # that do not use a versioned wrapper.
    if case_id is None:
        return None
    candidate = value.get(case_id)
    if candidate is None:
        # An explicit path API version without nodes is malformed rather than
        # a silently empty catalog.
        if "api_version" in value:
            return ExecutionPathSpec.from_mapping(value)
        return None
    if not isinstance(candidate, Mapping):
        raise ExecutionPathError("path for case %s must be an object" % case_id)
    return ExecutionPathSpec.from_mapping(candidate)


def evaluate_trace_conformance(
    spec: ExecutionPathSpec,
    trace: Sequence[Mapping[str, Any]],
    *,
    trace_complete: Optional[bool] = None,
) -> Mapping[str, Any]:
    """Evaluate semantic checkpoints, not an overfitted exact tool sequence."""

    if trace_complete is not None and not isinstance(trace_complete, bool):
        raise ExecutionPathError("trace_complete must be a boolean or None")
    if spec.read_only_compatibility:
        return {
            "api_version": TRACE_CONFORMANCE_API_VERSION,
            "status": "not_evaluable",
            "reason": "Path IR v3 was loaded through the read-only v1 compatibility layer; branch/retry/state semantics require a native evaluator",
            "trace_complete": trace_complete,
            "trace_completeness": (
                "complete" if trace_complete is True else "incomplete" if trace_complete is False else "unknown"
            ),
            "skill_patch_authorized": False,
            "coverage": None,
            "steps": [],
            "violations": [],
        }
    if spec.trace_completeness_required and trace_complete is not True:
        if trace_complete is False:
            reason = "trace was explicitly marked incomplete; execution-path evidence cannot be trusted"
            completeness_status = "incomplete"
        else:
            reason = "trace completeness was not provided; a non-empty trace is insufficient to prove completeness"
            completeness_status = "unknown"
        return {
            "api_version": TRACE_CONFORMANCE_API_VERSION,
            "status": "not_evaluable",
            "reason": reason,
            "trace_complete": trace_complete,
            "trace_completeness": completeness_status,
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
        "trace_complete": trace_complete,
        "trace_completeness": "complete" if trace_complete is True else "not_required",
        "skill_patch_authorized": True,
        "coverage": coverage,
        "steps": rows,
        "violations": violations,
    }


__all__ = [
    "EXECUTION_PATH_SPEC_API_VERSION",
    "EXECUTION_PATH_SPEC_V3_API_VERSION",
    "PATH_ANALYSIS_API_VERSION",
    "TRACE_CONFORMANCE_API_VERSION",
    "ExecutionPathError",
    "ExecutionPathSpec",
    "ExecutionStepSpec",
    "ExecutionPathInput",
    "evaluate_trace_conformance",
    "resolve_execution_path",
]
