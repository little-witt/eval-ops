"""Compile optimization workspaces into a UI-safe convergence graph."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit, urlunsplit


OPTIMIZATION_GRAPH_API_VERSION = "aceval.optimization-graph/v1"
SECRET_KEY_PARTS = ("secret", "token", "password", "authorization", "api_key", "apikey")
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/=-]{12,}|(?:sk|gh[pousr])[-_][a-z0-9_-]{12,}|"
    r"(?:token|password|passwd|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+)"
)
_URL_USERINFO = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)
_AUTH_HEADER = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:basic|bearer)\s+[^\s,;]+"
)
DEFAULT_CRITERIA = (
    "Output is strict JSON with a findings array",
    "All Oracle findings are reported",
    "Every finding references an existing changed source line",
    "No unsupported finding is reported",
    "Tool results prove the exact Skill, base, and head commits",
)


class OptimizationGraphError(ValueError):
    """Raised when a workspace cannot be compiled without inventing evidence."""


def _read_json(path: Path, *, required: bool = True) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return {}
        raise OptimizationGraphError("required graph input is missing: %s" % path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OptimizationGraphError("cannot read graph input: %s" % path) from exc
    if not isinstance(value, Mapping):
        raise OptimizationGraphError("graph input must be a JSON object: %s" % path)
    return value


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def _iteration_number(path: Path) -> int:
    match = re.fullmatch(r"iteration-(\d+)", path.name)
    return int(match.group(1)) if match else 0


def _iteration_dirs(root: Path) -> Tuple[Path, ...]:
    return tuple(sorted(
        (item for item in root.glob("iteration-*") if item.is_dir()),
        key=_iteration_number,
    ))


def _read_json_value(path: Path, *, required: bool = False) -> Any:
    """Read an arbitrary JSON document for optional evidence artifacts.

    ``_read_json`` intentionally accepts objects only because graph inputs are
    contracts. Session attempt logs and traces are arrays, however, and are
    still useful read-only evidence. This helper keeps malformed optional
    artifacts from taking down the entire console snapshot.
    """

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return None
        raise OptimizationGraphError("required graph input is missing: %s" % path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if required:
            raise OptimizationGraphError("cannot read graph input: %s" % path) from exc
        return None


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _strict_bool(value: Any) -> Optional[bool]:
    """Parse booleans without treating arbitrary non-empty strings as true.

    Evidence artifacts are external JSON.  A malformed value such as
    ``"false"`` must never satisfy a hard gate merely because Python's
    ``bool("false")`` is truthy.  Recognized textual forms are accepted for
    backwards compatibility; everything else remains unknown.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "y", "1"}:
            return True
        if normalized in {"false", "no", "n", "0"}:
            return False
    return None


def _trace_summary(session: Any) -> Mapping[str, Any]:
    """Return bounded, deterministic trace metadata without duplicating raw logs."""

    if not isinstance(session, Mapping):
        return {
            "event_count": 0,
            "tool_call_count": 0,
            "tool_result_count": 0,
            "error_count": 0,
            "kinds": {},
            "tools": {},
            "complete": None,
            "completeness": None,
        }
    observation = session.get("observation")
    observation = observation if isinstance(observation, Mapping) else {}
    raw_trace = observation.get("trace")
    if not isinstance(raw_trace, list):
        raw_trace = session.get("trace") if isinstance(session.get("trace"), list) else []
    kinds: Dict[str, int] = {}
    tools: Dict[str, int] = {}
    errors = 0
    tool_calls = 0
    tool_results = 0
    for event in raw_trace:
        if not isinstance(event, Mapping):
            continue
        kind = str(event.get("kind") or event.get("type") or "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind in ("tool_call", "tool", "tool_start"):
            tool_calls += 1
        if kind in ("tool_result", "tool_end"):
            tool_results += 1
        tool = event.get("tool") or event.get("name")
        if tool:
            name = str(tool)
            tools[name] = tools.get(name, 0) + 1
        if event.get("error"):
            errors += 1
    metadata = observation.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    completeness = session.get("completeness")
    if not isinstance(completeness, Mapping):
        completeness = metadata.get("observation_completeness")
    if not isinstance(completeness, Mapping):
        completeness = None
    trace_complete = (
        _strict_bool(completeness.get("trace"))
        if isinstance(completeness, Mapping) and "trace" in completeness
        else None
    )
    if metadata.get("trace_may_be_truncated") is True:
        trace_complete = False
    return {
        "event_count": len(raw_trace),
        "tool_call_count": tool_calls,
        "tool_result_count": tool_results,
        "error_count": errors,
        "kinds": dict(sorted(kinds.items())),
        "tools": dict(sorted(tools.items())),
        "complete": trace_complete,
        "completeness": completeness,
    }


def _expectation_ratio(expectations: Sequence[Any]) -> Optional[float]:
    rows = [
        item
        for item in expectations
        if isinstance(item, Mapping) and _strict_bool(item.get("passed")) is not None
    ]
    if not rows:
        return None
    return sum(_strict_bool(item.get("passed")) is True for item in rows) / len(rows)


def _score_dimensions(
    *,
    grading: Mapping[str, Any],
    timing: Mapping[str, Any],
    binding: Mapping[str, Any],
    path: Optional[Mapping[str, Any]],
    trace: Mapping[str, Any],
    attempts: Sequence[Any],
) -> Mapping[str, Any]:
    """Build an explainable per-run score vector.

    Only dimensions with observed evidence contribute to ``overall``. Cost
    and latency are reported as measurements, not silently converted into a
    pass/fail judgement because no policy threshold is present in a workspace.
    """

    summary = grading.get("summary") if isinstance(grading.get("summary"), Mapping) else {}
    formal = grading.get("formal_grade") if isinstance(grading.get("formal_grade"), Mapping) else {}
    expectations = grading.get("expectations") if isinstance(grading.get("expectations"), list) else []
    outcome = _number(summary.get("pass_rate"))
    expectation_ratio = _expectation_ratio(expectations)
    path_status = str(path.get("status")) if isinstance(path, Mapping) else None
    path_coverage = _number(path.get("coverage")) if isinstance(path, Mapping) else None
    binding_value = _strict_bool(grading.get("binding_verified"))
    if binding_value is None:
        binding_value = _strict_bool(binding.get("verified"))
    binding_measured = binding_value is not None
    trace_measured = trace.get("complete") is not None
    trace_complete = trace.get("complete") if trace_measured else None
    # The first expectation is the strict-output gate in the built-in review
    # pack. Use its text when available, but retain a generic format score for
    # custom packs that use a different label.
    format_row = next(
        (
            item
            for item in expectations
            if isinstance(item, Mapping)
            and any(token in str(item.get("text", "")).lower() for token in ("json", "format", "格式"))
        ),
        None,
    )
    if format_row is None and expectations:
        format_row = expectations[0] if isinstance(expectations[0], Mapping) else None
    format_score = (
        _strict_bool(format_row.get("passed"))
        if format_row is not None and "passed" in format_row
        else None
    )
    attempts_measured = bool(attempts)
    retry_count = max(0, len(attempts) - 1) if attempts_measured else 0

    dimensions: Dict[str, Mapping[str, Any]] = {
        "outcome": {
            "id": "outcome",
            "label": "结果准确性",
            "score": outcome,
            "measured": outcome is not None,
            "status": "pass" if outcome is not None and outcome >= 1 else "fail" if outcome is not None else "not_measured",
            "weight": 0.45,
        },
        "path": {
            "id": "path",
            "label": "执行路径符合度",
            "score": path_coverage,
            "measured": path_coverage is not None,
            "status": "pass" if path_status == "pass" else "fail" if path_status == "fail" else "not_evaluable" if path_status == "not_evaluable" else "not_measured",
            "weight": 0.25,
        },
        "format": {
            "id": "format",
            "label": "输出格式",
            "score": float(format_score) if format_score is not None else None,
            "measured": format_score is not None,
            "status": "pass" if format_score is True else "fail" if format_score is False else "not_measured",
            "weight": 0.15,
        },
        "binding": {
            "id": "binding",
            "label": "版本 / 仓库绑定",
            "score": 1.0 if binding_value is True else 0.0 if binding_measured else None,
            "measured": binding_measured,
            "status": "pass" if binding_value is True else "fail" if binding_measured else "not_measured",
            "weight": 0.15,
        },
        "efficiency": {
            "id": "efficiency",
            "label": "耗时与 Token",
            "score": None,
            "measured": bool(timing),
            "status": "measured" if timing else "not_measured",
            "tokens": int(_number(timing.get("total_tokens"), 0) or 0),
            "duration_seconds": _number(timing.get("total_duration_seconds"), 0.0) or 0.0,
            "weight": 0.0,
        },
        "reliability": {
            "id": "reliability",
            "label": "会话可靠性 / 重试",
            "score": (1.0 / len(attempts)) if attempts_measured else None,
            "measured": attempts_measured,
            "status": "pass" if attempts_measured and retry_count == 0 else "degraded" if attempts_measured else "not_measured",
            "attempt_count": len(attempts),
            "retry_count": retry_count,
            "weight": 0.0,
        },
        "evidence": {
            "id": "evidence",
            "label": "证据完整性",
            "score": 1.0 if trace_complete is True else 0.0 if trace_measured else None,
            "measured": trace_measured,
            "status": "pass" if trace_complete is True else "not_evaluable" if trace_measured else "not_measured",
            "event_count": int(trace.get("event_count", 0) or 0),
            "weight": 0.0,
        },
    }
    weighted = [
        (float(item["score"]), float(item["weight"]))
        for item in dimensions.values()
        if item.get("measured") and item.get("score") is not None and float(item.get("weight", 0)) > 0
    ]
    weight_total = sum(weight for _, weight in weighted)
    overall = sum(score * weight for score, weight in weighted) / weight_total if weight_total else None
    # Fail closed when a completed-looking artifact omits the primary outcome.
    # Optional path/binding/trace dimensions remain backward-compatible: if a
    # producer did not emit that artifact they are unknown rather than an
    # invented pass.
    hard_gate = (
        formal.get("status") == "pass"
        and outcome is not None
        and outcome >= 1.0
        and (path_status in (None, "pass"))
        and (binding_value in (None, True))
        and (trace_complete in (None, True))
    )
    return {
        "dimensions": dimensions,
        "overall": round(overall, 4) if overall is not None else None,
        "hard_gate": bool(hard_gate),
        "formula": "weighted mean of observed outcome/path/format/binding; efficiency and reliability are reported separately",
        "expectation_ratio": expectation_ratio,
    }


def _path_steps(path: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Normalize legacy ``steps`` and V3 Path IR ``nodes`` for read-only UI."""

    raw_steps = path.get("steps")
    uses_v3_nodes = not isinstance(raw_steps, list)
    if not isinstance(raw_steps, list):
        raw_steps = path.get("nodes") if isinstance(path.get("nodes"), list) else []
    normalized: List[Mapping[str, Any]] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, Mapping):
            continue
        step_id = str(raw.get("id") or raw.get("step_id") or "step-%02d" % (index + 1))
        kind = str(raw.get("kind") or raw.get("requiredness") or "required")
        label = str(raw.get("label") or raw.get("action") or step_id)
        # V3 order is authored in ``edges``.  Node array order is not an
        # execution constraint, even when a node omits predecessors.
        has_predecessor_field = uses_v3_nodes or "after" in raw or "predecessors" in raw
        after = raw.get("after")
        if not isinstance(after, list):
            after = raw.get("predecessors") if isinstance(raw.get("predecessors"), list) else []
        matcher = raw.get("match") if isinstance(raw.get("match"), Mapping) else {}
        if not matcher:
            # Path IR can describe a tool/command without the v1 matcher
            # object. Preserve these fields as display-only matcher hints.
            matcher = {}
            if raw.get("tool"):
                matcher["tool_name"] = raw.get("tool")
            observables = raw.get("observables")
            if isinstance(observables, list) and observables:
                matcher["contains"] = [str(item) for item in observables]
        normalized.append({
            "id": step_id,
            "label": label,
            "kind": kind,
            "match": matcher,
            "after": [str(item) for item in after],
            "after_explicit": has_predecessor_field,
            "alternative_group": raw.get("alternative_group") or raw.get("group"),
        })
    return normalized


def _path_graph(path: Optional[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """Convert a semantic path into a UI-friendly node/edge graph."""

    if not isinstance(path, Mapping):
        return None
    steps = _path_steps(path)
    nodes: List[Mapping[str, Any]] = []
    edges: List[Mapping[str, Any]] = []
    node_ids = {str(item.get("id")) for item in steps if item.get("id")}
    raw_edges = path.get("edges") if isinstance(path.get("edges"), list) else []
    authored_pairs = set()
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, Mapping):
            continue
        source = raw_edge.get("source") or raw_edge.get("from")
        target = raw_edge.get("target") or raw_edge.get("to")
        if source is None or target is None:
            continue
        source = str(source)
        target = str(target)
        if source not in node_ids or target not in node_ids:
            continue
        relation = raw_edge.get("kind") or raw_edge.get("relation") or raw_edge.get("type") or "edge"
        condition = raw_edge.get("when") or raw_edge.get("condition") or raw_edge.get("guard")
        edge = {"source": source, "target": target, "relation": str(relation)}
        if condition not in (None, ""):
            edge["condition"] = str(condition)
        edges.append(edge)
        authored_pairs.add((source, target))
    previous: Optional[str] = None
    for index, raw in enumerate(steps):
        step_id = str(raw.get("id") or "step-%02d" % (index + 1))
        after = [str(item) for item in raw.get("after", [])] if isinstance(raw.get("after"), list) else []
        nodes.append({
            "id": step_id,
            "label": str(raw.get("label") or step_id),
            "kind": str(raw.get("kind") or "required"),
            "match": raw.get("match") if isinstance(raw.get("match"), Mapping) else {},
            "after": after,
            "after_explicit": bool(raw.get("after_explicit")),
            "alternative_group": raw.get("alternative_group"),
        })
        # An explicit empty ``after`` means “no ordering constraint”; only a
        # legacy step that omits the field receives the visual sequence edge.
        predecessors = after if raw.get("after_explicit") else ([previous] if previous else [])
        for predecessor in predecessors:
            if predecessor and (predecessor, step_id) not in authored_pairs:
                edges.append({"source": predecessor, "target": step_id, "relation": "after" if after else "sequence"})
        previous = step_id
    def mermaid_text(value: Any) -> str:
        # Mermaid labels are presentation-only; escape punctuation so authored
        # Skill text cannot break the surrounding graph or inject markup.
        text = str(value or "").replace("\\", "\\\\").replace('"', "\\\"")
        return text.replace("\r", " ").replace("\n", " ")[:240]

    node_ids = {str(item["id"]): "n%03d" % index for index, item in enumerate(nodes, 1)}
    mermaid_lines = ["flowchart TD"]
    for node in nodes:
        node_id = node_ids[str(node["id"])]
        mermaid_lines.append('  %s["%s"]' % (node_id, mermaid_text(node["label"])))
    for edge in edges:
        source = node_ids.get(str(edge["source"]))
        target = node_ids.get(str(edge["target"]))
        if not source or not target:
            continue
        relation = mermaid_text(edge.get("relation"))
        condition = mermaid_text(edge.get("condition"))
        if condition:
            relation = "%s · %s" % (relation, condition) if relation else condition
        suffix = ('|%s|' % relation) if relation else ""
        mermaid_lines.append("  %s -->%s %s" % (source, suffix, target))
    return {
        "purpose": str(path.get("purpose") or ""),
        "nodes": nodes,
        "edges": edges,
        "mermaid": "\n".join(mermaid_lines),
        "step_count": len(nodes),
        "required_count": sum(item.get("kind") == "required" for item in nodes),
        "forbidden_count": sum(item.get("kind") == "forbidden" for item in nodes),
        "alternative_count": sum(item.get("kind") == "alternative" for item in nodes),
    }


def _path_catalog(iteration: Path, root: Path) -> Mapping[str, Mapping[str, Any]]:
    """Load optional execution path specs from known immutable artifacts."""

    catalog: Dict[str, Mapping[str, Any]] = {}
    candidates = (
        iteration / "execution-paths.json",
        iteration / "evaluation-design.json",
        root / "execution-paths.json",
        root / "evaluation-design.json",
    )
    for candidate in candidates:
        value = _read_json_value(candidate)
        if not isinstance(value, Mapping):
            continue
        paths = value.get("paths")
        if not isinstance(paths, Mapping):
            paths = value.get("execution_paths")
        if not isinstance(paths, Mapping):
            continue
        for case_id, path in paths.items():
            if isinstance(path, Mapping):
                catalog.setdefault(str(case_id), path)
    return catalog


def _safe_path_value(value: Any, key: str = "") -> Any:
    """Redact credential-like path matcher fields before embedding graph JSON."""

    lowered = key.casefold()
    if any(part in lowered for part in SECRET_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(item_key): _safe_path_value(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_safe_path_value(item, key) for item in value]
    if isinstance(value, str):
        value = _URL_USERINFO.sub(r"\1<redacted>@", value)
        value = _AUTH_HEADER.sub(r"\1<redacted>", value)
        generic_value_key = lowered in {"contains", "argv", "command", "value", "expected", "output", "stdout", "stderr"}
        marker_value = any(marker in value.casefold() for marker in ("secret", "password", "api_key", "access_token", "bearer "))
        if _SENSITIVE_VALUE.search(value) or (generic_value_key and marker_value and len(value) >= 8):
            return "<redacted>"
        if len(value) > 200:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
            return value[:200] + "… [sha256:" + digest + "]"
    return value


def _path_for_case(case_id: str, metadata: Mapping[str, Any], catalog: Mapping[str, Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    value = catalog.get(case_id)
    if value is None:
        for key in ("execution_path", "path", "path_spec"):
            candidate = metadata.get(key)
            if isinstance(candidate, Mapping):
                value = candidate
                break
    if not isinstance(value, Mapping):
        return None
    # JSON round-trip gives callers a detached, deterministic mapping and
    # prevents accidental mutation of the loaded design document.
    try:
        return _safe_path_value(json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False)))
    except (TypeError, ValueError):
        return None


def _run_payload(run_dir: Path) -> Mapping[str, Any]:
    if not run_dir:
        return {"status": "unavailable"}
    request = run_dir / "request.json"
    grading_path = run_dir / "grading.json"
    timing_path = run_dir / "timing.json"
    binding_path = run_dir / "binding.json"
    path_path = run_dir / "path_conformance.json"
    session_path = run_dir / "outputs" / "session.json"
    attempts_path = run_dir / "outputs" / "attempts.json"
    if grading_path.is_file():
        status = "completed"
    elif request.is_file():
        status = "running"
    else:
        status = "pending"
    grading = _read_json(grading_path, required=False)
    timing = _read_json(timing_path, required=False)
    binding = _read_json(binding_path, required=False)
    path = _read_json(path_path, required=False) if path_path.is_file() else None
    session = _read_json_value(session_path)
    attempts_value = _read_json_value(attempts_path)
    attempts = attempts_value if isinstance(attempts_value, list) else []
    summary = grading.get("summary") if isinstance(grading.get("summary"), Mapping) else {}
    formal = grading.get("formal_grade") if isinstance(grading.get("formal_grade"), Mapping) else {}
    trace = _trace_summary(session)
    scores = _score_dimensions(
        grading=grading,
        timing=timing,
        binding=binding,
        path=path,
        trace=trace,
        attempts=attempts,
    )
    total_tokens = int(_number(timing.get("total_tokens"), 0) or 0)
    duration_seconds = _number(timing.get("total_duration_seconds"), 0.0) or 0.0
    return {
        "status": status,
        "pass_rate": _number(summary.get("pass_rate"), 0.0) or 0.0,
        "formal_pass": formal.get("status") == "pass",
        "tokens": total_tokens,
        "duration_seconds": duration_seconds,
        # Preserve a fail-closed boolean for legacy consumers while the score
        # vector above uses the typed value and can distinguish unknown data.
        "binding_verified": _strict_bool(
            grading.get("binding_verified", binding.get("verified"))
        ) is True,
        "expectations": grading.get("expectations", []) if isinstance(grading.get("expectations"), list) else [],
        "skill_commit": str(binding.get("skill_commit", "")),
        "session_id": str(binding.get("session_id", "")),
        # Evidence and score fields are intentionally additive to v1. Existing
        # consumers can continue to use pass_rate/formal_pass while newer UIs
        # can render path, trace completeness, retries and dimension scores.
        "path_conformance": path,
        "path_status": path.get("status") if isinstance(path, Mapping) else None,
        "path_coverage": _number(path.get("coverage")) if isinstance(path, Mapping) else None,
        "trace_summary": trace,
        # ``None`` means no trace/completeness evidence was emitted; do not
        # collapse it into an explicit incomplete result in the read model.
        "trace_complete": trace.get("complete"),
        "log_completeness": trace.get("completeness"),
        "attempt_count": len(attempts),
        "retry_count": max(0, len(attempts) - 1),
        "score": scores,
        "score_dimensions": scores["dimensions"],
        "evidence": {
            "request": str(request),
            "grading": str(grading_path),
            "timing": str(timing_path),
            "binding": str(binding_path),
            "path": str(path_path),
            "output": str(run_dir / "outputs" / "final_output.txt"),
            "session": str(session_path),
            "attempts": str(attempts_path),
        },
    }


def _case_group(case_id: str) -> str:
    if case_id.startswith("ts-web-"):
        return "TypeScript Web"
    if case_id.startswith("rn-"):
        return "React Native"
    if case_id.startswith("mini-"):
        return "微信小程序"
    if case_id.startswith("java-"):
        return "Java Backend"
    return "Other"


def _case_type(case_id: str) -> str:
    return "clean-control" if "clean" in case_id else "defect"


def _configuration_for(eval_dir: Path, preferred: str) -> Optional[Path]:
    exact = eval_dir / preferred / "run-1"
    if exact.is_dir():
        return exact
    return None


def _collect_cases(iteration: Path, root: Path) -> List[Mapping[str, Any]]:
    cases = []
    path_catalog = _path_catalog(iteration, root)
    for eval_dir in sorted(iteration.glob("eval-*")):
        metadata = _read_json(eval_dir / "eval_metadata.json", required=False)
        case_id = str(metadata.get("eval_name") or eval_dir.name)
        case_path = _path_for_case(case_id, metadata, path_catalog)
        baseline_dir = _configuration_for(eval_dir, "old_skill")
        candidate_dir = _configuration_for(eval_dir, "with_skill")
        baseline = _run_payload(baseline_dir) if baseline_dir else {"status": "unavailable"}
        candidate = _run_payload(candidate_dir) if candidate_dir else {"status": "unavailable"}
        for payload in (baseline, candidate):
            evidence = payload.get("evidence")
            if isinstance(evidence, Mapping):
                payload["evidence"] = {
                    key: _relative(Path(value), root)
                    for key, value in evidence.items()
                }
        assertions = metadata.get("assertions")
        cases.append({
            "id": case_id,
            "eval_id": metadata.get("eval_id"),
            "group": _case_group(case_id),
            "type": _case_type(case_id),
            "prompt": str(metadata.get("prompt", "")),
            "assertions": list(assertions) if isinstance(assertions, list) else [],
            # Keep the authored semantic path separate from observed
            # conformance. This lets the UI show the intended route and the
            # actual trace verdict side by side without conflating outcome and
            # process correctness.
            "path": case_path,
            "path_graph": _path_graph(case_path),
            "path_source": (
                "execution-paths.json"
                if case_id in path_catalog
                else "eval_metadata.json"
                if case_path is not None
                else None
            ),
            "baseline": baseline,
            "candidate": candidate,
        })
    return cases


def _aggregate_run(iteration: Path, configuration: str) -> Mapping[str, Any]:
    values = []
    for eval_dir in sorted(iteration.glob("eval-*")):
        run_dir = _configuration_for(eval_dir, configuration)
        if run_dir:
            values.append(_run_payload(run_dir))
    completed = [item for item in values if item["status"] == "completed"]
    path_measured = [item for item in completed if item.get("path_coverage") is not None]
    path_passes = sum(item.get("path_status") == "pass" for item in path_measured)
    scored = [
        item for item in completed
        if isinstance(item.get("score"), Mapping)
        and item.get("score", {}).get("overall") is not None
    ]
    trace_measured = [item for item in completed if item.get("trace_complete") is not None]
    trace_complete = sum(item.get("trace_complete") is True for item in trace_measured)
    return {
        "cases": len(values),
        "completed": len(completed),
        "pass_rate": (
            sum(float(item["pass_rate"]) for item in completed) / len(completed)
            if completed else None
        ),
        "formal_passes": sum(bool(item["formal_pass"]) for item in completed),
        "strict_json_passes": sum(
            bool(
                item["expectations"]
                and _strict_bool(item["expectations"][0].get("passed")) is True
            )
            for item in completed
        ),
        "tokens": sum(int(item["tokens"]) for item in completed),
        "duration_seconds": sum(float(item["duration_seconds"]) for item in completed),
        "bindings_verified": sum(bool(item["binding_verified"]) for item in completed),
        "skill_commit": next((item["skill_commit"] for item in completed if item["skill_commit"]), ""),
        "path_measured": len(path_measured),
        "path_passes": path_passes,
        "path_pass_rate": path_passes / len(path_measured) if path_measured else None,
        "path_coverage": (
            sum(float(item["path_coverage"]) for item in path_measured) / len(path_measured)
            if path_measured else None
        ),
        "score_measured": len(scored),
        "score_mean": (
            sum(float(item["score"]["overall"]) for item in scored) / len(scored)
            if scored else None
        ),
        "trace_complete": trace_complete,
        "trace_measured": len(trace_measured),
        "retry_count": sum(int(item.get("retry_count", 0) or 0) for item in completed),
        "attempt_count": sum(int(item.get("attempt_count", 0) or 0) for item in completed),
    }


def _dimensions(cases: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    labels: List[str] = []
    for case in cases:
        for side in ("baseline", "candidate"):
            payload = case.get(side)
            if not isinstance(payload, Mapping):
                continue
            for expectation in payload.get("expectations", []):
                text = str(expectation.get("text", ""))
                if text and text not in labels:
                    labels.append(text)
            score_dimensions = payload.get("score_dimensions")
            if isinstance(score_dimensions, Mapping):
                for dimension in score_dimensions.values():
                    if not isinstance(dimension, Mapping):
                        continue
                    text = str(dimension.get("label") or dimension.get("id") or "")
                    # Efficiency and reliability are measurements rather than
                    # quality gates; include them in the case score vector but
                    # do not turn absent policy thresholds into a misleading
                    # comparison bar.
                    weight = _number(dimension.get("weight"), 0.0) or 0.0
                    if text and text not in labels and weight > 0:
                        labels.append(text)
    if not labels:
        labels = list(DEFAULT_CRITERIA)
    result = []
    for index, label in enumerate(labels):
        row: Dict[str, Any] = {"id": "dimension-%02d" % (index + 1), "label": label}
        for side in ("baseline", "candidate"):
            passed = 0
            measured = 0
            evidence = []
            for case in cases:
                payload = case.get(side)
                expectations = payload.get("expectations", []) if isinstance(payload, Mapping) else []
                match = next((item for item in expectations if item.get("text") == label), None)
                if match is not None:
                    expectation_passed = _strict_bool(match.get("passed"))
                    if expectation_passed is None:
                        continue
                    measured += 1
                    passed += expectation_passed is True
                    if expectation_passed is not True:
                        evidence.append({"case_id": case["id"], "detail": str(match.get("evidence", ""))})
                    continue
                score_dimensions = payload.get("score_dimensions", {}) if isinstance(payload, Mapping) else {}
                if not isinstance(score_dimensions, Mapping):
                    continue
                score_match = next(
                    (
                        item for item in score_dimensions.values()
                        if isinstance(item, Mapping) and str(item.get("label") or item.get("id") or "") == label
                    ),
                    None,
                )
                if score_match is None or not score_match.get("measured"):
                    continue
                measured += 1
                passed += score_match.get("status") == "pass"
                if score_match.get("status") != "pass":
                    evidence.append({"case_id": case["id"], "detail": str(score_match.get("status") or "not measured")})
            row[side] = {
                "passed": passed,
                "measured": measured,
                "rate": passed / measured if measured else None,
                "failures": evidence,
            }
        candidate_rate = row["candidate"]["rate"]
        baseline_rate = row["baseline"]["rate"]
        if candidate_rate is None or baseline_rate is None:
            status = "not_measured"
        elif candidate_rate > baseline_rate:
            status = "improved"
        elif candidate_rate < baseline_rate:
            status = "regressed"
        else:
            status = "unchanged"
        row["status"] = status
        result.append(row)
    return result


def _safe_reference(value: Any) -> Mapping[str, Any]:
    text = str(value or "")
    if text.startswith("$"):
        return {"source": "environment", "name": text[1:], "configured": True}
    return {"source": "profile", "configured": bool(text), "value": text if text else None}


def _safe_url(value: Any) -> str:
    text = str(value or "")
    try:
        parts = urlsplit(text)
    except ValueError:
        return "configured" if text else ""
    host = parts.hostname or ""
    if parts.port:
        host += ":%d" % parts.port
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


def safe_profile_summary(profile_path: Optional[Union[str, Path]]) -> Mapping[str, Any]:
    if not profile_path:
        return {"configured": False, "repositories": []}
    path = Path(profile_path).expanduser().resolve()
    profile = _read_json(path)
    repositories = []
    raw_repositories = profile.get("repositories")
    if not isinstance(raw_repositories, list):
        raw_repositories = []
    for item in raw_repositories:
        if not isinstance(item, Mapping):
            continue
        authorization = item.get("authorization_token")
        authorization_env = item.get("authorization_token_env")
        authorization_source = (
            str(authorization)[1:]
            if str(authorization).startswith("$")
            else str(authorization_env or ("profile-masked" if authorization else ""))
        )
        repositories.append({
            "type": str(item.get("type", "repository")),
            "url": _safe_url(item.get("url")),
            "mount_path": str(item.get("mount_path", "")),
            "authorization_configured": bool(authorization or authorization_env),
            "authorization_source": authorization_source or None,
        })
    agent = profile.get("agent")
    if not agent and profile.get("agent_id_env"):
        agent = "$" + str(profile["agent_id_env"])
    environment = profile.get("environment_id")
    if not environment and profile.get("environment_id_env"):
        environment = "$" + str(profile["environment_id_env"])
    return {
        "configured": True,
        "profile_name": str(profile.get("name") or path.stem),
        "api_version": str(profile.get("api_version", "")),
        "base_url": _safe_url(profile.get("base_url")),
        "agent": _safe_reference(agent),
        "environment": _safe_reference(environment),
        "vault_count": len(profile.get("vault_ids", [])) if isinstance(profile.get("vault_ids"), list) else 0,
        "credentials_file_configured": bool(profile.get("credentials_file")),
        "repositories": repositories,
    }


def _plan_iteration(plan: Mapping[str, Any], version: str) -> Mapping[str, Any]:
    values = plan.get("iterations")
    if not isinstance(values, list):
        return {}
    return next(
        (item for item in values if isinstance(item, Mapping) and item.get("version") == version),
        {},
    )


def _node_metrics(iteration: Path, configuration: str) -> Mapping[str, Any]:
    metrics = _aggregate_run(iteration, configuration)
    rate = metrics.get("pass_rate")
    return {
        "assertion_pass_rate": rate,
        "formal_passes": metrics["formal_passes"],
        "cases": metrics["completed"],
        "strict_json_passes": metrics["strict_json_passes"],
        "tokens": metrics["tokens"],
        "duration_seconds": metrics["duration_seconds"],
        "bindings_verified": metrics["bindings_verified"],
        "path_pass_rate": metrics.get("path_pass_rate"),
        "path_coverage": metrics.get("path_coverage"),
        "score_mean": metrics.get("score_mean"),
        "trace_complete": metrics.get("trace_complete", 0),
        "trace_measured": metrics.get("trace_measured", 0),
        "retry_count": metrics.get("retry_count", 0),
        "attempt_count": metrics.get("attempt_count", 0),
    }


def compile_optimization_graph(
    workspace: Union[str, Path],
    *,
    plan_path: Optional[Union[str, Path]] = None,
    profile_path: Optional[Union[str, Path]] = None,
) -> Mapping[str, Any]:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise OptimizationGraphError("optimization workspace does not exist: %s" % root)
    history = _read_json(root / "history.json")
    plan = _read_json(Path(plan_path).expanduser().resolve(), required=False) if plan_path else {}
    iterations = _iteration_dirs(root)
    if not iterations:
        raise OptimizationGraphError("workspace has no iteration directories")
    latest = iterations[-1]
    cases = _collect_cases(latest, root)
    dimensions = _dimensions(cases)
    history_items = history.get("iterations")
    if not isinstance(history_items, list) or not history_items:
        raise OptimizationGraphError("history.json has no iterations")

    input_node = {
        "id": "input",
        "parent_id": None,
        "kind": "input",
        "status": "completed",
        "title": "Skill 输入与成功标准",
        "summary": str(plan.get("goal") or "Improve the Skill against frozen evaluation criteria."),
        "why": "Freeze the optimization target and promotion contract before changing the Skill.",
        "plan": list(plan.get("success_criteria", DEFAULT_CRITERIA)),
        "decision": "frozen",
        "next_steps": ["Generate or select cases without changing the target Skill."],
        "metrics": {},
        "evidence_refs": [_relative(root / "history.json", root)],
    }
    design_node = {
        "id": "evaluation-design",
        "parent_id": "input",
        "kind": "evaluation_design",
        "status": "completed",
        "title": "自动评测设计",
        "summary": "%d cases across %d stacks and %d grading dimensions."
        % (len(cases), len({case["group"] for case in cases}), len(dimensions)),
        "why": "Use the same frozen cases and assertions for baseline and every candidate.",
        "plan": ["Defect coverage", "Clean false-positive controls", "Exact commit binding"],
        "decision": "frozen",
        "next_steps": ["Run the immutable baseline."],
        "metrics": {"cases": len(cases), "dimensions": len(dimensions)},
        "evidence_refs": [
            _relative(root / "evals" / "evals.json", root),
            _relative(latest, root),
        ],
    }
    nodes: List[Mapping[str, Any]] = [input_node, design_node]
    edges: List[Mapping[str, str]] = [
        {"source": "input", "target": "evaluation-design", "relation": "defines"}
    ]
    current_best = str(history.get("current_best", "v0"))
    for index, item in enumerate(history_items):
        if not isinstance(item, Mapping):
            continue
        version = str(item.get("version", "v%d" % index))
        parent = str(item.get("parent") or "evaluation-design")
        iteration = iterations[min(index, len(iterations) - 1)]
        configuration = "old_skill" if index == 0 else "with_skill"
        metrics = _node_metrics(iteration, configuration)
        plan_item = _plan_iteration(plan, version)
        grading_result = str(item.get("grading_result", "unknown"))
        if version == current_best:
            status = "current_best"
        elif grading_result == "won":
            status = "promoted"
        elif grading_result == "lost":
            status = "rejected"
        elif grading_result == "tie":
            status = "needs_review"
        else:
            status = "completed"
        commit = _aggregate_run(iteration, configuration).get("skill_commit", "")
        title = str(plan_item.get("title") or ("Frozen baseline" if index == 0 else "Candidate %s" % version))
        nodes.append({
            "id": version,
            "parent_id": parent,
            "kind": "baseline" if index == 0 else "candidate",
            "status": status,
            "title": title,
            "summary": str(plan_item.get("hypothesis") or (
                "Measure the original Skill without candidate changes."
                if index == 0 else "Evaluate a candidate against the frozen baseline."
            )),
            "why": str(plan_item.get("evidence") or "Use grading evidence from the previous node."),
            "plan": list(plan_item.get("changes", [])),
            "decision": str(plan_item.get("decision") or grading_result),
            "next_steps": list(plan_item.get("next_steps", [])),
            "metrics": metrics,
            "commit": commit,
            "iteration": _iteration_number(iteration),
            "evidence_refs": [
                _relative(iteration / ("suite-%s.json" % configuration), root),
                _relative(iteration, root),
            ],
        })
        edges.append({
            "source": parent,
            "target": version,
            "relation": "baseline" if index == 0 else "candidate",
        })

    latest_version = str(history_items[-1].get("version", "v%d" % (len(history_items) - 1)))
    convergence_plan = plan.get("convergence") if isinstance(plan.get("convergence"), Mapping) else {}
    baseline_metrics = next(node["metrics"] for node in nodes if node["id"] == "v0")
    latest_metrics = next(node["metrics"] for node in nodes if node["id"] == latest_version)
    blockers = list(convergence_plan.get("blockers", []))
    if latest_metrics.get("assertion_pass_rate") is not None and baseline_metrics.get("assertion_pass_rate") is not None:
        if latest_metrics["assertion_pass_rate"] < baseline_metrics["assertion_pass_rate"]:
            blockers.append("Candidate assertion pass rate is below the frozen baseline.")
    repetitions = int(plan.get("repetitions", 1) or 1)
    if repetitions < 3:
        blockers.append("Only one run per case/configuration; stochastic variance is not established.")
    if latest_metrics.get("strict_json_passes", 0) < latest_metrics.get("cases", 0):
        blockers.append("Final-response format is not stable across all cases.")
    blockers = list(dict.fromkeys(str(item) for item in blockers if str(item)))
    decision = "promote" if latest_version == current_best and not blockers else "do_not_promote"
    next_steps = list(convergence_plan.get("next_steps", [])) or [
        "Repeat each case at least three times and measure format compliance separately.",
        "Fix the Agent structured-output boundary if content is correct but prose precedes JSON.",
        "Promote only when hard gates pass without lowering the primary quality metric.",
    ]
    nodes.append({
        "id": "convergence",
        "parent_id": latest_version,
        "kind": "convergence",
        "status": "blocked" if blockers else "completed",
        "title": "收敛决策",
        "summary": "Candidate is not promoted." if decision != "promote" else "Candidate is ready to promote.",
        "why": "Apply the frozen promotion and stopping rules instead of selecting by narrative preference.",
        "plan": list(plan.get("promotion_rules", [])),
        "decision": decision,
        "next_steps": next_steps,
        "metrics": latest_metrics,
        "blockers": blockers,
        "evidence_refs": [
            _relative(root / "history.json", root),
            _relative(latest / "benchmark.json", root),
        ],
    })
    edges.append({"source": latest_version, "target": "convergence", "relation": "decides"})

    any_running = any(
        case[side].get("status") == "running"
        for case in cases for side in ("baseline", "candidate")
        if isinstance(case.get(side), Mapping)
    )
    path_cases = [case for case in cases if isinstance(case.get("path_graph"), Mapping)]
    path_analysis = {
        "case_count": len(cases),
        "cases_with_paths": len(path_cases),
        "coverage_rate": len(path_cases) / len(cases) if cases else 0.0,
        "cases": [
            {
                "case_id": case["id"],
                "prompt": case.get("prompt", ""),
                "source": case.get("path_source"),
                "path": case.get("path"),
                "graph": case.get("path_graph"),
                "baseline": {
                    "status": case.get("baseline", {}).get("path_status") if isinstance(case.get("baseline"), Mapping) else None,
                    "coverage": case.get("baseline", {}).get("path_coverage") if isinstance(case.get("baseline"), Mapping) else None,
                },
                "candidate": {
                    "status": case.get("candidate", {}).get("path_status") if isinstance(case.get("candidate"), Mapping) else None,
                    "coverage": case.get("candidate", {}).get("path_coverage") if isinstance(case.get("candidate"), Mapping) else None,
                },
            }
            for case in cases
        ],
    }
    return {
        "api_version": OPTIMIZATION_GRAPH_API_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(root),
        "run": {
            "id": str(plan.get("id") or history.get("skill_name") or root.name),
            "skill_name": str(history.get("skill_name") or plan.get("skill_name") or root.name),
            "status": "running" if any_running else ("ready" if decision == "promote" else "needs_revalidation"),
            "confidence": "low" if repetitions < 3 else "measured",
            "repetitions": repetitions,
        },
        "input": {
            "skill": {
                "name": str(history.get("skill_name") or plan.get("skill_name") or root.name),
                "source": str(plan.get("skill_source", "")),
                "ref": str(plan.get("skill_ref", "")),
                "current_best": current_best,
            },
            "goal": str(plan.get("goal") or input_node["summary"]),
            "success_criteria": list(plan.get("success_criteria", DEFAULT_CRITERIA)),
            "promotion_rules": list(plan.get("promotion_rules", [])),
        },
        "evaluation_design": {
            "origin": str(plan.get("case_origin", "system-generated reusable Fixture Lab")),
            "frozen": True,
            "case_count": len(cases),
            "groups": sorted({case["group"] for case in cases}),
            "cases": cases,
            # Aliases make the read model easy to consume from both the legacy
            # static console and the desktop renderer while retaining the
            # authored path separately from observed run conformance.
            "paths": {case["id"]: case["path"] for case in cases if case.get("path") is not None},
            "path_graphs": {case["id"]: case["path_graph"] for case in cases if case.get("path_graph") is not None},
            "dimensions": [item["label"] for item in dimensions],
        },
        "path_analysis": path_analysis,
        "nodes": nodes,
        "edges": edges,
        "dimension_summary": dimensions,
        "convergence": {
            "current_best": current_best,
            "latest_candidate": latest_version,
            "decision": decision,
            "blockers": blockers,
            "next_steps": next_steps,
            "rules": list(plan.get("promotion_rules", [])),
        },
        "configuration": safe_profile_summary(profile_path),
    }


def write_optimization_graph(
    workspace: Union[str, Path],
    output: Union[str, Path],
    *,
    plan_path: Optional[Union[str, Path]] = None,
    profile_path: Optional[Union[str, Path]] = None,
) -> Path:
    target = Path(output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    graph = compile_optimization_graph(
        workspace, plan_path=plan_path, profile_path=profile_path
    )
    target.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return target


__all__ = [
    "OPTIMIZATION_GRAPH_API_VERSION",
    "OptimizationGraphError",
    "compile_optimization_graph",
    "safe_profile_summary",
    "write_optimization_graph",
]
