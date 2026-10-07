"""Token-bounded, cross-case analysis for remote Skill evaluations."""

from __future__ import annotations

from collections import Counter
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .agent_runtime import ModelClient
from .contracts import GradeResult, GradeStatus, RunObservation, as_primitive
from .execution_path import ExecutionPathSpec, evaluate_trace_conformance
from .graders import builtin_graders
from .kernel_contracts import ANALYSIS_DECISION_API_VERSION
from .kernel_v2 import build_case_aggregates, compile_diagnosis_graph
from .pack import EvalPackLoader
from .pack_lifecycle import pack_calibration_status


class EvidenceAnalysisError(RuntimeError):
    pass


def skill_entrypoint_path(inventory: Optional[Sequence[str]] = None) -> str:
    """Return the editable Skill entrypoint used by the current checkout.

    Evaluation workspaces may expose the subject as either ``SKILL.md`` or
    ``src/SKILL.md``.  Analysis used to fall back to the former even when the
    latter was the only editable resource, which made an otherwise valid
    proposal fail the scope gate after the user approved it.
    """

    values = []
    for item in inventory or ():
        value = str(item or "").strip()
        if value and value not in values:
            values.append(value)
    for candidate in ("SKILL.md", "src/SKILL.md"):
        if candidate in values:
            return candidate
    for value in values:
        if value.replace("\\", "/").rstrip("/").split("/")[-1] == "SKILL.md":
            return value
    return "SKILL.md"


def normalize_skill_path(path: Any, inventory: Optional[Sequence[str]] = None) -> str:
    """Normalize the legacy root entrypoint alias against an editable scope."""

    value = str(path or "").strip()
    allowed = {str(item) for item in (inventory or ()) if str(item).strip()}
    if value == "SKILL.md" and value not in allowed and "src/SKILL.md" in allowed:
        return "src/SKILL.md"
    return value


def _read_json(path: str) -> Mapping[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceAnalysisError("cannot read case run artifact") from exc
    if not isinstance(value, Mapping):
        raise EvidenceAnalysisError("case run artifact must be an object")
    return value


def _normalize_output(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _strict_true(value: Any) -> bool:
    """Observation completeness is a typed contract, not a truthy flag."""

    return value is True


def _exact_expected(case: Mapping[str, Any], output: Any) -> Optional[bool]:
    if "expected_output" not in case:
        return None
    metadata = case.get("metadata", {})
    if isinstance(metadata, Mapping) and metadata.get("expectation_mode") not in (None, "exact"):
        return None
    return _normalize_output(output) == case.get("expected_output")


def _optimization_case_ready(case: Mapping[str, Any]) -> bool:
    """Only trusted, executable Cases may authorize a Skill mutation.

    Draft/model-proposed Cases remain useful for exploration and evidence
    collection, but they cannot make the system rewrite the subject that the
    same model is evaluating.
    """

    metadata = case.get("metadata") if isinstance(case.get("metadata"), Mapping) else {}
    expectation_mode = metadata.get("expectation_mode")
    if expectation_mode == "model_proposed":
        return False
    if (
        expectation_mode == "evalpack_graders"
        and metadata.get("evalpack_lifecycle") not in ("frozen", "legacy")
    ):
        return False
    aceval_test = metadata.get("aceval_test") if isinstance(metadata.get("aceval_test"), Mapping) else {}
    if aceval_test:
        trusted = (
            aceval_test.get("oracle_ready") is True
            and aceval_test.get("executable", True) is True
            and aceval_test.get("needs_user_input", False) is not True
            and aceval_test.get("oracle_trust")
            in ("deterministic", "seed_derived", "reference_differential", "human_confirmed")
        )
        # Semantic criteria are eligible only after a person has explicitly
        # confirmed the concrete observable pass conditions shown in the UI.
        # Model-proposed semantic criteria remain exploratory.
        if expectation_mode == "semantic":
            return trusted and aceval_test.get("oracle_trust") == "human_confirmed"
        return trusted
    return (
        "expected_output" in case
        and metadata.get("expectation_mode") in (None, "exact")
        and metadata.get("oracle_ready", True) is not False
        and metadata.get("oracle_trust") not in ("model_proposed", "unobservable")
    )


def _trace_summary(trace: Sequence[Mapping[str, Any]], maximum_chars: int) -> Mapping[str, Any]:
    kinds = Counter(str(event.get("kind") or event.get("type") or "unknown") for event in trace)
    tools = Counter(str(event.get("tool") or event.get("name") or "") for event in trace if event.get("tool") or event.get("name"))
    errors = []
    evidence = []
    remaining = maximum_chars
    candidates = []
    for index, event in enumerate(trace):
        if event.get("error"):
            errors.append({"index": index, "error": str(event.get("error"))[:800]})
        kind = str(event.get("kind") or event.get("type") or "")
        # CATX receipts use provider-specific Agent event names rather than
        # the compact tool_call/tool_result names used by the legacy path IR.
        # Keep those events in the bounded evidence index so deterministic
        # goal checks and the semantic reviewer can see the actual command
        # and final report, instead of inferring from a tool-count summary.
        if kind not in (
            "tool_result", "tool_call", "tool", "tool_start",
            "agent.tool_use", "assistant.tool_use", "function_call",
            "agent.tool_result", "assistant.tool_result",
            "agent.message", "assistant.message", "agent.output", "assistant.output",
        ) or remaining <= 0:
            continue
        text = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        # Prefer executable commands and high-signal report markers over the
        # first few setup messages.  A bounded window that only contains
        # todo/list_dir events can otherwise hide the later script invocation
        # and make a successful Case look like it skipped the required step.
        signal = 3
        if any(
            isinstance(event.get(key), str) and event.get(key).strip()
            for key in ("command", "argv")
        ) or any(
            isinstance(event.get(key), Mapping)
            and (isinstance(event[key].get("command"), str) or isinstance(event[key].get("argv"), (list, tuple)))
            for key in ("input", "payload", "arguments")
        ):
            signal = 0
        elif any(marker in text for marker in ("analyze_complexity.js", "git diff", "SKILL.md", "行数", "风险", "必须修复", "快速检查清单")):
            signal = 1
        elif kind in {"agent.message", "assistant.message", "agent.output", "assistant.output"}:
            signal = 2
        candidates.append((signal, index, text))
    candidates.sort(key=lambda item: (item[0], item[1]))
    for _, index, text in candidates:
        clipped = text[: min(900, remaining)]
        evidence.append({"index": index, "text": clipped})
        remaining -= len(clipped)
        if remaining <= 0:
            break
    return {"event_count": len(trace), "kinds": dict(kinds), "tools": dict(tools), "errors": errors[:8], "selected_evidence": evidence}


async def _evaluate_evalpack_graders(
    pack: Any,
    scenario: Any,
    observation: RunObservation,
) -> Sequence[GradeResult]:
    graders = builtin_graders()
    results = []
    for grader_id in scenario.grader_ids:
        spec = pack.manifest.grader_by_id(grader_id)
        grader = graders.get(spec.type)
        if grader is None:
            results.append(
                GradeResult(
                    grader_id=grader_id,
                    status=GradeStatus.ERROR,
                    hard=spec.hard,
                    message="trusted built-in Grader is not registered: %s" % spec.type,
                )
            )
            continue
        params = dict(spec.params)
        params.update(scenario.scenario.grader_params.get(grader_id, {}))
        params.update(
            {
                "hard": spec.hard,
                "pack_root": str(pack.root),
                "resources": pack.resources,
            }
        )
        try:
            value = grader.evaluate(observation, scenario.oracle, params)
            if inspect.isawaitable(value):
                value = await value
            if not isinstance(value, GradeResult):
                raise TypeError("Grader returned an invalid result")
            value = GradeResult(
                grader_id=grader_id,
                status=value.status,
                version=value.version or str(getattr(grader, "version", "")),
                score=value.score,
                hard=spec.hard,
                metrics=value.metrics,
                evidence=value.evidence,
                missing=value.missing,
                message=value.message,
            )
        except Exception as exc:
            value = GradeResult(
                grader_id=grader_id,
                status=GradeStatus.ERROR,
                version=str(getattr(grader, "version", "")),
                hard=spec.hard,
                message="grader_error: %s" % exc,
            )
        results.append(value)
    return results


def _run_coroutine(value: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    # ``compact_case_evidence`` is intentionally synchronous. If a caller is
    # already in an event loop, isolate the built-in grader coroutine instead
    # of nesting that loop or silently skipping deterministic grading.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, value).result()


def _evalpack_grading(
    case: Mapping[str, Any],
    run: Mapping[str, Any],
    evalpack_ref: Optional[str],
) -> Optional[Mapping[str, Any]]:
    metadata = case.get("metadata") if isinstance(case.get("metadata"), Mapping) else {}
    scenario_id = metadata.get("evalpack_scenario_id")
    if not evalpack_ref or not isinstance(scenario_id, str) or not scenario_id:
        return None
    try:
        pack = EvalPackLoader().load(evalpack_ref)
        scenario = next(item for item in pack.scenarios if item.id == scenario_id)
    except Exception as exc:
        return {
            "status": "not_evaluable",
            "reason": "EvalPack or scenario cannot be loaded: %s" % exc,
            "pack_ref": str(evalpack_ref),
            "scenario_id": scenario_id,
            "grades": [],
        }
    lifecycle = pack_calibration_status(pack)
    expected_pack_hash = metadata.get("evalpack_pack_hash")
    expected_scenario_hash = metadata.get("evalpack_scenario_hash")
    if (
        isinstance(expected_pack_hash, str)
        and expected_pack_hash
        and expected_pack_hash != pack.pack_hash
    ) or (
        isinstance(expected_scenario_hash, str)
        and expected_scenario_hash
        and expected_scenario_hash != scenario.content_hash
    ):
        return {
            "status": "not_evaluable",
            "reason": "EvalPack or scenario changed after the evaluation design was frozen",
            "pack_ref": str(pack.root),
            "pack_hash": pack.pack_hash,
            "pack_lifecycle": lifecycle,
            "scenario_id": scenario.id,
            "scenario_hash": scenario.content_hash,
            "grader_ids": list(scenario.grader_ids),
            "grades": [],
        }
    if lifecycle not in ("frozen", "legacy"):
        # Draft/generated Graders are useful design material, but they have not
        # passed the Case/Suite calibration gate and therefore cannot override
        # an explicit trusted expectation or a bounded semantic verdict.
        return {
            "status": "not_calibrated",
            "reason": "EvalPack Graders are still draft and are not authoritative",
            "pack_ref": str(pack.root),
            "pack_hash": pack.pack_hash,
            "pack_lifecycle": lifecycle,
            "scenario_id": scenario.id,
            "scenario_hash": scenario.content_hash,
            "grader_ids": list(scenario.grader_ids),
            "grades": [],
        }
    session = run.get("session") if isinstance(run.get("session"), Mapping) else {}
    raw = session.get("observation") if isinstance(session.get("observation"), Mapping) else {}
    observation = RunObservation(
        output=raw.get("output"),
        trace=tuple(raw.get("trace", ())) if isinstance(raw.get("trace", ()), Sequence) and not isinstance(raw.get("trace", ()), (str, bytes)) else (),
        artifacts=raw.get("artifacts", {}) if isinstance(raw.get("artifacts", {}), Mapping) else {},
        pre_state=raw.get("pre_state") if isinstance(raw.get("pre_state"), Mapping) else None,
        post_state=raw.get("post_state") if isinstance(raw.get("post_state"), Mapping) else None,
        error=raw.get("error"),
        usage=raw.get("usage", {}) if isinstance(raw.get("usage", {}), Mapping) else {},
        metadata=raw.get("metadata", {}) if isinstance(raw.get("metadata", {}), Mapping) else {},
    )
    results = list(_run_coroutine(_evaluate_evalpack_graders(pack, scenario, observation)))
    statuses = {item.status for item in results}
    if not results or GradeStatus.ERROR in statuses or GradeStatus.NOT_EVALUABLE in statuses:
        status = "not_evaluable"
    elif GradeStatus.FAIL in statuses:
        status = "fail"
    else:
        status = "pass"
    grades = []
    for item in results:
        value = dict(as_primitive(item))
        try:
            value["grader_type"] = pack.manifest.grader_by_id(item.grader_id).type
        except Exception:
            value["grader_type"] = "unknown"
        grades.append(value)
    return {
        "status": status,
        "reason": (
            "all selected EvalPack Graders passed"
            if status == "pass"
            else "one or more EvalPack Graders failed"
            if status == "fail"
            else "one or more EvalPack Graders could not evaluate the evidence"
        ),
        "pack_ref": str(pack.root),
        "pack_hash": pack.pack_hash,
        "pack_lifecycle": lifecycle,
        "scenario_id": scenario.id,
        "scenario_hash": scenario.content_hash,
        "grader_ids": list(scenario.grader_ids),
        "grades": grades,
    }


def compact_case_evidence(
    case: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    max_chars: int,
    path_spec: Optional[Mapping[str, Any]] = None,
    evalpack_ref: Optional[str] = None,
) -> Mapping[str, Any]:
    artifact = row.get("artifact")
    if not artifact:
        # A completed-looking transport row without an immutable artifact is
        # still an unevaluable attempt.  Keep a typed, visible record here so
        # a semantic model cannot turn missing evidence into a passing Case.
        return {
            "case_id": case.get("id"),
            "attempt_number": row.get("attempt_number"),
            "attempts": [dict(item) for item in row.get("attempts", ()) if isinstance(item, Mapping)],
            "retry_count": max(0, len(row.get("attempts", ())) - 1) if isinstance(row.get("attempts"), list) else 0,
            "timing": {
                "started_at": row.get("started_at"),
                "completed_at": row.get("completed_at"),
                "duration_ms": row.get("duration_ms"),
            },
            "run_status": row.get("status"),
            "terminal": row.get("terminal"),
            "error": row.get("error") or "remote attempt did not produce an immutable session artifact",
            "binding_status": row.get("binding_status"),
            "binding_evidence": row.get("binding_evidence"),
            "output_excerpt": "",
            "output_chars": 0,
            "exact_expected_match": None,
            "formal_grading": None,
            "usage": {},
            "trace": {"event_count": 0, "kinds": {}, "tools": {}, "errors": [], "selected_evidence": []},
            "trace_complete": False,
            "log_completeness": {
                "complete": False,
                "missing_required_event_types": [],
                "reason_codes": ["attempt.artifact_missing"],
                "event_count": 0,
                "page_count": 0,
                "sequence_status": "not_provided",
                "event_log_sha256": None,
                "hash_verified": False,
                "raw_events": False,
                "legacy_event_log": False,
                "promotion_eligible": False,
            },
            "path_conformance": None,
            "artifact": None,
            "source": str(row.get("source") or "") or None,
        }
    run = _read_json(str(artifact))
    session = run.get("session", {}) if isinstance(run.get("session"), Mapping) else {}
    observation = session.get("observation", {}) if isinstance(session.get("observation"), Mapping) else {}
    trace = observation.get("trace", ())
    # CATX receipts carry the authoritative immutable event log at the run
    # envelope's ``events`` field.  Prefer it over the abbreviated observation
    # trace so every Case review is grounded in the complete session sequence;
    # the archived artifact still remains the source for deeper windows.
    raw_event_log = run.get("events")
    if isinstance(raw_event_log, list) and raw_event_log:
        trace = raw_event_log
    if not isinstance(trace, Sequence) or isinstance(trace, (str, bytes)):
        trace = ()
    trace_items = tuple(item for item in trace if isinstance(item, Mapping))
    completeness = session.get("completeness", {}) if isinstance(session.get("completeness"), Mapping) else {}
    observation_metadata = observation.get("metadata", {}) if isinstance(observation.get("metadata"), Mapping) else {}
    log_completeness = run.get("log_completeness") if isinstance(run.get("log_completeness"), Mapping) else {}
    event_integrity = observation_metadata.get("event_log_integrity") if isinstance(observation_metadata.get("event_log_integrity"), Mapping) else {}
    if not log_completeness and event_integrity:
        log_completeness = dict(event_integrity)
    session_source = str(session.get("source") or run.get("source") or "")
    # Versioned CATX artifacts are required to carry the immutable raw event
    # snapshot and its seal.  Unversioned, non-CATX fixtures remain readable
    # only through an explicit legacy marker emitted by the collector (or the
    # historical unversioned fixture shape); they are never confused with a
    # production CATX receipt in the UI.
    production_artifact = session_source in {"catx_session_api", "catx"} or run.get("api_version") == "aceval.kernel-case-run/v1"
    legacy_event_log = bool(
        run.get("legacy_event_log")
        or (isinstance(log_completeness, Mapping) and log_completeness.get("legacy_event_log") is True)
        or session.get("legacy_event_log") is True
    )
    if not production_artifact and not legacy_event_log and "api_version" not in run:
        # This is the compatibility shape used by old local/synthetic run
        # fixtures.  Keep the marker visible so it cannot silently pass as a
        # CATX artifact if it is later copied into a real task.
        legacy_event_log = True
    missing_event_types = list(log_completeness.get("missing_required_event_types", ())) if isinstance(log_completeness.get("missing_required_event_types", ()), Sequence) and not isinstance(log_completeness.get("missing_required_event_types", ()), (str, bytes)) else []
    reason_codes = list(log_completeness.get("reason_codes", ())) if isinstance(log_completeness.get("reason_codes", ()), Sequence) and not isinstance(log_completeness.get("reason_codes", ()), (str, bytes)) else []
    events = run.get("events") if isinstance(run.get("events"), list) else None
    recorded_hash = log_completeness.get("event_log_sha256")
    hash_verified = None
    if events is not None and isinstance(recorded_hash, str) and recorded_hash:
        canonical = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        hash_verified = hashlib.sha256(canonical).hexdigest() == recorded_hash
        if not hash_verified and "event_log_hash_mismatch" not in reason_codes:
            reason_codes.append("event_log_hash_mismatch")
    if production_artifact and not legacy_event_log:
        if events is None and "event_log_missing" not in reason_codes:
            reason_codes.append("event_log_missing")
        if not isinstance(recorded_hash, str) or not recorded_hash:
            if "event_log_hash_missing" not in reason_codes:
                reason_codes.append("event_log_hash_missing")
    integrity_complete = log_completeness.get("complete")
    if integrity_complete is None:
        integrity_complete = not missing_event_types and not reason_codes
    if production_artifact and not legacy_event_log and reason_codes:
        integrity_complete = False
    trace_channel_complete = _strict_true(completeness.get("trace"))
    if not trace_channel_complete and "trace_channel_incomplete" not in reason_codes:
        reason_codes.append("trace_channel_incomplete")
    trace_complete = (
        trace_channel_complete
        and observation_metadata.get("trace_may_be_truncated") is not True
        and integrity_complete is not False
        and not missing_event_types
        and hash_verified is not False
        and (not production_artifact or legacy_event_log or (events is not None and isinstance(recorded_hash, str) and bool(recorded_hash)))
    )
    path_result = None
    if path_spec:
        path_result = evaluate_trace_conformance(ExecutionPathSpec.from_mapping(path_spec), trace_items, trace_complete=trace_complete)
    output = observation.get("output")
    output_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
    output_limit = max(300, min(max_chars // 2, 2400))
    exact = _exact_expected(case, output)
    formal_grading = _evalpack_grading(case, run, evalpack_ref)
    return {
        "case_id": case.get("id"),
        "attempt_number": row.get("attempt_number"),
        "attempts": [dict(item) for item in row.get("attempts", ()) if isinstance(item, Mapping)],
        "retry_count": max(0, len(row.get("attempts", ())) - 1) if isinstance(row.get("attempts"), list) else 0,
        "timing": {
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"),
            "duration_ms": row.get("duration_ms"),
        },
        "run_status": row.get("status"),
        "terminal": row.get("terminal"),
        "error": observation.get("error") or row.get("error"),
        "binding_status": row.get("binding_status"),
        "binding_evidence": row.get("binding_evidence"),
        "output_excerpt": output_text[:output_limit],
        "output_chars": len(output_text),
        "exact_expected_match": exact,
        "formal_grading": formal_grading,
        "usage": observation.get("usage", {}),
        "trace": _trace_summary(trace_items, max_chars - output_limit),
        "trace_complete": trace_complete,
        "log_completeness": {
            "complete": integrity_complete is True and trace_channel_complete and hash_verified is not False,
            "missing_required_event_types": missing_event_types,
            "reason_codes": reason_codes,
            "event_count": log_completeness.get("event_count", len(events) if events is not None else len(trace_items)),
            "page_count": log_completeness.get("page_count"),
            "sequence_status": log_completeness.get("sequence_status"),
            "event_log_sha256": recorded_hash,
            "hash_verified": hash_verified,
            "raw_events": events is not None,
            "legacy_event_log": legacy_event_log,
            "promotion_eligible": not (production_artifact and legacy_event_log),
        },
        "path_conformance": path_result,
        "artifact": str(artifact),
        "source": session_source or None,
    }


def _heuristic_case_result(case: Mapping[str, Any], evidence: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    trace = evidence.get("trace") if isinstance(evidence.get("trace"), Mapping) else {}
    output_chars = int(evidence.get("output_chars") or 0)
    event_count = int(trace.get("event_count") or 0)
    execution_observed = evidence.get("trace_complete") is True and event_count > 0 and output_chars > 0
    # A terminal/error status is not allowed to erase a completed execution
    # trace.  Once the Case branch, Skill mount, review events and output are
    # present, the run is an analyzable failure (possibly caused by the
    # Agent/tool), not an ``evidence insufficient`` Case.
    if (evidence.get("run_status") != "completed" or evidence.get("error")) and not execution_observed:
        return {"case_id": case.get("id"), "status": "not_evaluable", "reason": str(evidence.get("error") or "remote run failed before a complete review trace was produced"), "evidence_refs": [evidence.get("artifact")]}
    log_completeness = evidence.get("log_completeness")
    if isinstance(log_completeness, Mapping) and log_completeness.get("complete") is False and not execution_observed:
        reasons = list(log_completeness.get("reason_codes", ()))
        missing = list(log_completeness.get("missing_required_event_types", ()))
        detail = ", ".join(str(item) for item in reasons + missing) or "complete session log was not proven"
        return {"case_id": case.get("id"), "status": "not_evaluable", "reason": "session evidence is incomplete: %s" % detail, "evidence_refs": [evidence.get("artifact")]}
    path = evidence.get("path_conformance")
    if isinstance(path, Mapping) and path.get("status") == "not_evaluable" and not execution_observed:
        return {"case_id": case.get("id"), "status": "not_evaluable", "reason": str(path.get("reason")), "evidence_refs": [evidence.get("artifact")]}
    if isinstance(path, Mapping) and path.get("status") == "fail":
        return {"case_id": case.get("id"), "status": "fail", "reason": "execution path violates the Skill contract", "evidence_refs": [evidence.get("artifact")]}
    # Path conformance is a hard contract. A deterministic outcome Grader may
    # add dimensions, but it must never turn a path violation into a passing
    # Case or hide an incomplete trace from mutation authorization.
    formal = evidence.get("formal_grading")
    if isinstance(formal, Mapping):
        formal_status = str(formal.get("status") or "not_evaluable")
        formal_reason = str(formal.get("reason") or "")
        formal_integrity_gap = any(
            token in formal_reason.casefold()
            for token in ("changed after", "cannot be loaded", "could not evaluate", "hash", "scenario")
        )
        if formal_status in ("pass", "fail") or (formal_status == "not_evaluable" and (not execution_observed or formal_integrity_gap)):
            return {
                "case_id": case.get("id"),
                "status": formal_status,
                "reason": str(formal.get("reason") or "EvalPack deterministic Graders"),
                "evidence_refs": [evidence.get("artifact")],
            }
        metadata = case.get("metadata") if isinstance(case.get("metadata"), Mapping) else {}
        if (
            formal_status == "not_calibrated"
            and metadata.get("expectation_mode") == "evalpack_graders"
            and not execution_observed
        ):
            return {
                "case_id": case.get("id"),
                "status": "not_evaluable",
                "reason": str(
                    formal.get("reason")
                    or "EvalPack Graders have not completed calibration"
                ),
                "evidence_refs": [evidence.get("artifact")],
            }
    exact = evidence.get("exact_expected_match")
    if exact is True:
        return {"case_id": case.get("id"), "status": "pass", "reason": "output exactly matches the user-provided expected result", "evidence_refs": [evidence.get("artifact")]}
    if exact is False:
        return {"case_id": case.get("id"), "status": "fail", "reason": "output does not match the user-provided expected result", "evidence_refs": [evidence.get("artifact")]}
    # A Case with a complete immutable session is still evaluable even when it
    # has no exact-output Oracle.  The session proves the Agent loaded the
    # mounted Skill, ran the requested review and produced a response; that is
    # the execution fact the analysis phase must explain.  A configured local
    # semantic model may subsequently replace this provisional status, but the
    # control plane must never turn an otherwise valid run into a spurious
    # "evidence insufficient" result merely because the output is open-ended.
    if execution_observed:
        return {
            "case_id": case.get("id"),
            "status": "pass",
            "reason": "会话已完成并形成完整 Trace/输出；开放结果交由逐 Case 分析核对目标与评分维度",
            "evidence_refs": [evidence.get("artifact")],
        }
    return {
        "case_id": case.get("id"),
        "status": "fail",
        "reason": "会话虽已结束，但没有形成可核对的输出或执行事件；按执行效果记录为未达标",
        "evidence_refs": [evidence.get("artifact")],
    }


def _evidence_issue(
    case_id: str,
    result: Mapping[str, Any],
    aggregate: Any,
) -> Mapping[str, Any]:
    """Turn an opaque not-evaluable verdict into an actionable recovery item."""

    reason = str(result.get("reason") or "No evaluable Attempt was produced")
    validity_codes = sorted({
        str(code)
        for attempt in getattr(aggregate, "attempts", ())
        for code in attempt.evidence_validity.reason_codes
    })
    missing_channels = sorted({
        str(channel)
        for attempt in getattr(aggregate, "attempts", ())
        for channel in attempt.evidence_validity.missing_channels
    })
    searchable = " ".join([reason, *validity_codes, *missing_channels]).casefold()
    reason_text = reason.casefold()
    if any(token in reason_text for token in ("timeout", "gateway", "remote run failed", "infrastructure", "connection", "transport")):
        category = "remote_or_analysis_failure"
        evidence_type = "remote_session_failed"
        reason_cn = "远端会话没有成功完成，当前不能把失败归因到 Skill。"
        action = "analyze_existing_trace"
        guidance = "保留本次完整会话，直接基于原始 Trace 分析失败原因；不要重复创建远端会话。"
        retryable = False
    elif any(token in validity_codes for token in ("attempt.artifact_missing", "attempt.case_session_binding_missing")) or "artifact" in reason_text and "missing" in reason_text:
        # A transport row can be marked completed while the immutable receipt
        # was never attached to this Case.  This is a binding problem, not a
        # semantic failure and must be shown as such in the UI.
        category = "repository_binding"
        evidence_type = "case_session_binding_missing"
        reason_cn = "Case 没有绑定到对应的不可变会话产物，当前无法确认这份日志是否属于该 Case。"
        action = "analyze_existing_trace"
        guidance = "核对 Case、Attempt、测试分支和会话产物绑定后，直接在原始证据上分析；不要重跑会话。"
        retryable = False
    elif any(token in searchable for token in ("oracle", "expected", "grader", "calibrat", "trusted", "判定", "预期", "insufficient evidence", "not enough evidence", "cannot determine", "无法判断", "证据不足")):
        category = "oracle_not_ready"
        evidence_type = "semantic_or_oracle_insufficient"
        reason_cn = "会话证据已记录，但缺少可信的通过标准或语义判定依据，不能安全得出通过/失败结论。"
        action = "reopen_case_review"
        guidance = "补充或确认可观察的通过标准后，回到本次 Trace 重新分析；不重新创建远端会话。"
        retryable = False
    elif any(token in searchable for token in ("binding", "commit", "repository", "mount")):
        category = "repository_binding"
        evidence_type = "case_session_binding_missing"
        reason_cn = "没有证明会话对应了该 Case 的测试分支、base/head commit 或仓库挂载。"
        action = "analyze_existing_trace"
        guidance = "核对 Skill 与 PR 的 base/head commit 绑定，并在现有 Trace 上完成归因；不重新创建会话。"
        retryable = False
    elif any(token in searchable for token in ("log", "event", "trace", "hash", "sequence", "artifact", "channel", "complete")):
        category = "evidence_incomplete"
        evidence_type = "session_log_incomplete"
        reason_cn = "会话日志或 Trace 不完整，缺少可核验的事件、序列或完整性收据。"
        action = "analyze_existing_trace"
        guidance = "远端任务可能已完成；保留当前日志并标记缺口，优先分析现有 Trace，不重复创建会话。"
        retryable = False
    else:
        category = "remote_or_analysis_failure"
        evidence_type = "remote_session_failed"
        reason_cn = "没有形成可用于评分的完整会话证据，暂不能区分远端故障与 Skill 问题。"
        action = "analyze_existing_trace"
        guidance = "保留当前会话证据，先分析失败原因；如确实属于传输故障，由用户显式发起独立评测流程。"
        retryable = False
    return {
        "case_id": case_id,
        "category": category,
        "evidence_type": evidence_type,
        "reason": reason,
        "reason_cn": reason_cn,
        "reason_codes": validity_codes,
        "missing_channels": missing_channels,
        "recommended_action": action,
        "guidance": guidance,
        "targeted_retry_available": retryable,
    }


def _parse_model_decision(
    content: str,
    case_ids: Sequence[str],
    *,
    baseline_case_ids: Sequence[str] = (),
) -> Mapping[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise EvidenceAnalysisError("local analysis Agent returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise EvidenceAnalysisError("local analysis Agent must return an object")
    required = ("case_results", "failure_clusters", "conflicts", "proposed_changes", "target_scope")
    if any(key not in value for key in required):
        raise EvidenceAnalysisError("local analysis Agent omitted required decision fields")
    results = value.get("case_results")
    if not isinstance(results, list) or any(not isinstance(item, Mapping) for item in results):
        raise EvidenceAnalysisError("analysis case_results must be an array of objects")
    returned = {str(item.get("case_id")) for item in results}
    if returned != set(case_ids):
        raise EvidenceAnalysisError("analysis case_results do not cover the selected cases")
    for item in results:
        if item.get("status") not in ("pass", "fail", "not_evaluable"):
            raise EvidenceAnalysisError("analysis case status is invalid")
    for field in ("failure_clusters", "conflicts", "proposed_changes", "target_scope"):
        items = value.get(field)
        if not isinstance(items, list):
            raise EvidenceAnalysisError("analysis %s must be an array" % field)
    if any(not isinstance(item, str) or not item for item in value.get("target_scope", ())):
        raise EvidenceAnalysisError("analysis target_scope must contain non-empty paths")
    if baseline_case_ids:
        baseline_results = value.get("without_skill_baseline_case_results")
        if not isinstance(baseline_results, list) or any(not isinstance(item, Mapping) for item in baseline_results):
            raise EvidenceAnalysisError("analysis without_skill_baseline_case_results must be an array of objects")
        returned_baseline = {str(item.get("case_id")) for item in baseline_results}
        if returned_baseline != set(baseline_case_ids):
            raise EvidenceAnalysisError("analysis baseline results do not cover the unresolved baseline cases")
        if any(item.get("status") not in ("pass", "fail", "not_evaluable") for item in baseline_results):
            raise EvidenceAnalysisError("analysis baseline case status is invalid")
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


class CrossCaseAnalyzer:
    """Combines deterministic checks with at most one model call per batch."""

    def __init__(self, model: Optional[ModelClient], *, max_prompt_chars: int = 48_000, max_evidence_chars_per_case: int = 4000, required_k: int = 2) -> None:
        if isinstance(required_k, bool) or not isinstance(required_k, int) or not 1 <= required_k <= 20:
            raise ValueError("required_k must be between 1 and 20")
        self.model = model
        self.max_prompt_chars = max_prompt_chars
        self.max_evidence_chars_per_case = max_evidence_chars_per_case
        self.required_k = required_k

    def analyze(
        self,
        *,
        task_id: str,
        iteration: int,
        goal: str,
        standards: Sequence[str],
        cases: Sequence[Mapping[str, Any]],
        primary_batch: Mapping[str, Any],
        path_specs: Optional[Mapping[str, Mapping[str, Any]]] = None,
        verification_batch: Optional[Mapping[str, Any]] = None,
        comparison_baseline_batch: Optional[Mapping[str, Any]] = None,
        capability_summary: Optional[Mapping[str, Any]] = None,
        editable_resource_inventory: Optional[Sequence[str]] = None,
        evalpack_ref: Optional[str] = None,
    ) -> Mapping[str, Any]:
        entrypoint = skill_entrypoint_path(editable_resource_inventory)
        rows = {str(item.get("case_id")): item for item in primary_batch.get("cases", ()) if isinstance(item, Mapping)}
        verification_rows = {
            str(item.get("case_id")): item
            for item in (verification_batch.get("cases", ()) if verification_batch else ())
            if isinstance(item, Mapping)
        }
        baseline_rows = {
            str(item.get("case_id")): item
            for item in (comparison_baseline_batch.get("cases", ()) if comparison_baseline_batch else ())
            if isinstance(item, Mapping)
        }
        evidence = []
        verification_evidence = []
        baseline_evidence = []
        baseline_heuristic = {}
        verification_heuristic = {}
        heuristic = {}
        for case in cases:
            case_id = str(case.get("id"))
            item = compact_case_evidence(case, rows.get(case_id, {}), max_chars=self.max_evidence_chars_per_case, path_spec=(path_specs or {}).get(case_id), evalpack_ref=evalpack_ref)
            evidence.append(item)
            result = _heuristic_case_result(case, item)
            if result is not None:
                heuristic[case_id] = result
            if verification_batch is not None:
                verify_item = compact_case_evidence(
                    case,
                    verification_rows.get(case_id, {}),
                    max_chars=self.max_evidence_chars_per_case,
                    path_spec=(path_specs or {}).get(case_id),
                    evalpack_ref=evalpack_ref,
                )
                verification_evidence.append(verify_item)
                verify_result = _heuristic_case_result(case, verify_item)
                if verify_result is not None:
                    verification_heuristic[case_id] = verify_result
            if comparison_baseline_batch is not None:
                baseline_item = compact_case_evidence(
                    case,
                    baseline_rows.get(case_id, {}),
                    max_chars=self.max_evidence_chars_per_case,
                    path_spec=None,
                    evalpack_ref=evalpack_ref,
                )
                baseline_evidence.append(baseline_item)
                baseline_result = _heuristic_case_result(case, baseline_item)
                if baseline_result is not None:
                    baseline_heuristic[case_id] = baseline_result

        unresolved = [str(case.get("id")) for case in cases if str(case.get("id")) not in heuristic]
        baseline_unresolved = [
            str(case.get("id"))
            for case in cases
            if comparison_baseline_batch is not None and str(case.get("id")) not in baseline_heuristic
        ]
        deterministic_results = dict(heuristic)
        model_usage: Mapping[str, Any] = {}
        serialized_prompt_chars = 0
        needs_model_analysis = bool(
            unresolved
            or baseline_unresolved
            or any(item.get("status") == "fail" for item in deterministic_results.values())
            or any(item.get("status") == "fail" for item in verification_heuristic.values())
            # Open-ended/semantic Cases need a model pass even when the
            # execution-fact fallback is present.  Otherwise a candidate and
            # its without-Skill baseline would both look like provisional
            # passes and incremental value could never be established.
            or any(
                "expected_output" not in case
                or (
                    isinstance(case.get("metadata"), Mapping)
                    and case.get("metadata", {}).get("expectation_mode") in ("semantic", "model_proposed")
                )
                for case in cases
            )
        )
        model_called = False
        if needs_model_analysis:
            if self.model is None:
                for case_id in unresolved:
                    item = next(entry for entry in evidence if entry["case_id"] == case_id)
                    heuristic[case_id] = {"case_id": case_id, "status": "not_evaluable", "reason": "no trusted expected result and no local analysis Agent is configured", "evidence_refs": [item.get("artifact")]}
            else:
                request = {
                    "goal": goal,
                    "standards": list(standards),
                    "capability_summary": capability_summary or {},
                    "editable_resource_inventory": list(editable_resource_inventory or (entrypoint,)),
                    "cases": [dict(case) for case in cases],
                    "evidence": evidence,
                    "verification_evidence": verification_evidence,
                    "without_skill_baseline_evidence": baseline_evidence,
                    "instructions": {
                        "cross_case": "Analyze all cases together; do not optimize one case at the expense of another.",
                        "grounding": "Use only supplied evidence; not_evaluable is preferable to guessing.",
                        "changes": "Propose the smallest coherent Skill resource changes; use exact paths from editable_resource_inventory and do not embed case literals.",
                        "baseline": "Judge unresolved without-Skill baseline cases independently; do not credit the candidate for value unless the baseline actually fails.",
                    },
                    "output_contract": {
                        "case_results": [{"case_id": "id", "status": "pass|fail|not_evaluable", "verification_status": "pass|fail|not_run", "reason": "short", "evidence_refs": []}],
                        "failure_clusters": [{"id": "cluster", "case_ids": [], "root_cause": "", "skill_change_authorized": True}],
                        "conflicts": [{"case_ids": [], "description": "", "resolution": ""}],
                        "proposed_changes": [{"target": "exact/editable/file", "change": "", "why": "", "case_ids": []}],
                        "target_scope": ["one or more exact paths from editable_resource_inventory"],
                        "without_skill_baseline_case_results": [
                            {"case_id": case_id, "status": "pass|fail|not_evaluable", "reason": "short", "evidence_refs": []}
                            for case_id in baseline_unresolved
                        ],
                    },
                }
                serialized = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                if len(serialized) > self.max_prompt_chars:
                    # Drop selected tool-result excerpts first; immutable
                    # artifact paths preserve access for a later deep dive.
                    for item in evidence + verification_evidence + baseline_evidence:
                        if isinstance(item.get("trace"), dict):
                            item["trace"]["selected_evidence"] = []
                    request["evidence"] = evidence
                    request["verification_evidence"] = verification_evidence
                    request["without_skill_baseline_evidence"] = baseline_evidence
                    serialized = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                if len(serialized) > self.max_prompt_chars:
                    raise EvidenceAnalysisError("compacted analysis request still exceeds Token budget")
                serialized_prompt_chars = len(serialized)
                reply = self.model.complete(
                    [{"role": "system", "content": "You are the local Skill evaluation analyst. Return strict JSON only. Separate infrastructure uncertainty from Skill defects and minimize changes."}, {"role": "user", "content": serialized}],
                    (),
                )
                decision = _parse_model_decision(
                    reply.content,
                    [str(case.get("id")) for case in cases],
                    baseline_case_ids=baseline_unresolved,
                )
                model_called = True
                heuristic = {str(item["case_id"]): dict(item) for item in decision["case_results"]}
                # Exact user expectations, formal EvalPack results, and
                # deterministic path failures are authoritative.  The
                # complete-trace fallback is deliberately *provisional* for
                # open-ended Cases, so a semantic model may still distinguish
                # a grounded candidate from a weaker without-Skill baseline.
                authoritative = {}
                for case in cases:
                    case_id = str(case.get("id"))
                    item = next((entry for entry in evidence if str(entry.get("case_id")) == case_id), {})
                    value = deterministic_results.get(case_id)
                    if value is None:
                        continue
                    path = item.get("path_conformance") if isinstance(item.get("path_conformance"), Mapping) else {}
                    formal = item.get("formal_grading") if isinstance(item.get("formal_grading"), Mapping) else {}
                    if value.get("status") == "not_evaluable" or isinstance(item.get("exact_expected_match"), bool) or path.get("status") == "fail" or formal.get("status") in ("pass", "fail"):
                        authoritative[case_id] = value
                heuristic.update(authoritative)
                model_baseline = {
                    str(item["case_id"]): dict(item)
                    for item in decision.get("without_skill_baseline_case_results", ())
                }
                # As with the candidate, keep the complete-trace fallback
                # provisional for semantic baseline Cases.  Only explicit
                # expected output, formal grading, or a path failure may
                # override the model's baseline judgement.
                baseline_authoritative = {}
                for case in cases:
                    case_id = str(case.get("id"))
                    item = next((entry for entry in baseline_evidence if str(entry.get("case_id")) == case_id), {})
                    value = baseline_heuristic.get(case_id)
                    if value is None:
                        continue
                    path = item.get("path_conformance") if isinstance(item.get("path_conformance"), Mapping) else {}
                    formal = item.get("formal_grading") if isinstance(item.get("formal_grading"), Mapping) else {}
                    if value.get("status") == "not_evaluable" or isinstance(item.get("exact_expected_match"), bool) or path.get("status") == "fail" or formal.get("status") in ("pass", "fail"):
                        baseline_authoritative[case_id] = value
                model_baseline.update(baseline_authoritative)
                baseline_heuristic = model_baseline
                failure_clusters = list(decision["failure_clusters"])
                conflicts = list(decision["conflicts"])
                proposed_changes = list(decision["proposed_changes"])
                target_scope = [normalize_skill_path(item, editable_resource_inventory or (entrypoint,)) for item in decision["target_scope"]]
                proposed_changes = [
                    dict(item, target=normalize_skill_path(item.get("target"), editable_resource_inventory or (entrypoint,)))
                    if isinstance(item, Mapping) and item.get("target") else dict(item)
                    for item in proposed_changes
                ]
                model_usage = dict(reply.usage)
        if not model_called:
            for case_id in baseline_unresolved:
                item = next(entry for entry in baseline_evidence if entry["case_id"] == case_id)
                baseline_heuristic[case_id] = {
                    "case_id": case_id,
                    "status": "not_evaluable",
                    "reason": "no trusted expected result and no local analysis Agent is configured",
                    "evidence_refs": [item.get("artifact")],
                }
            failed_ids = [case_id for case_id, result in heuristic.items() if result["status"] == "fail"]
            failure_clusters = ([{"id": "observed-failures", "case_ids": failed_ids, "root_cause": "requires cross-case Skill repair analysis", "skill_change_authorized": bool(failed_ids)}] if failed_ids else [])
            conflicts = []
            proposed_changes = ([{"target": entrypoint, "change": "repair the shared cause supported by failed case evidence", "why": "one or more frozen expectations failed", "case_ids": failed_ids}] if failed_ids else [])
            target_scope = [entrypoint] if failed_ids else []

        optimization_eligible_case_ids = {
            str(case.get("id")) for case in cases if _optimization_case_ready(case)
        }
        directly_failed_eligible_case_ids = {
            case_id
            for case_id, result in heuristic.items()
            if case_id in optimization_eligible_case_ids and result.get("status") == "fail"
        }
        normalized_clusters = []
        for cluster in failure_clusters:
            if not isinstance(cluster, Mapping):
                continue
            value = dict(cluster)
            original_case_ids = [str(item) for item in value.get("case_ids", ())]
            eligible_ids = [item for item in original_case_ids if item in directly_failed_eligible_case_ids]
            value["case_ids"] = eligible_ids
            value["excluded_unready_case_ids"] = [item for item in original_case_ids if item not in optimization_eligible_case_ids]
            value["excluded_non_failure_case_ids"] = [
                item
                for item in original_case_ids
                if item in optimization_eligible_case_ids
                and item not in directly_failed_eligible_case_ids
            ]
            value["skill_change_authorized"] = bool(value.get("skill_change_authorized") is True and eligible_ids)
            normalized_clusters.append(value)
        failure_clusters = normalized_clusters
        normalized_changes = []
        for change in proposed_changes:
            if not isinstance(change, Mapping):
                continue
            value = dict(change)
            original_case_ids = [str(item) for item in value.get("case_ids", ())]
            eligible_ids = [item for item in original_case_ids if item in directly_failed_eligible_case_ids]
            if not eligible_ids:
                continue
            value["case_ids"] = eligible_ids
            value["excluded_unready_case_ids"] = [item for item in original_case_ids if item not in optimization_eligible_case_ids]
            value["excluded_non_failure_case_ids"] = [
                item
                for item in original_case_ids
                if item in optimization_eligible_case_ids
                and item not in directly_failed_eligible_case_ids
            ]
            normalized_changes.append(value)
        proposed_changes = normalized_changes
        if not proposed_changes:
            target_scope = []

        results = [heuristic[str(case.get("id"))] for case in cases]
        primary_passes = [item["case_id"] for item in results if item["status"] == "pass"]

        # V2 control-plane records are compiled after semantic analysis. The
        # model may contribute semantic verdicts and hypotheses, but evidence
        # validity, hard path failures, reliability aggregation, and graph
        # identity are deterministic.
        verification_results = {}
        if verification_batch is not None:
            for case in cases:
                case_id = str(case.get("id"))
                deterministic_verification = verification_heuristic.get(case_id)
                if deterministic_verification is not None:
                    verification_results[case_id] = deterministic_verification
                    continue
                semantic_status = heuristic.get(case_id, {}).get("verification_status")
                if semantic_status in ("pass", "fail", "not_evaluable"):
                    verification_results[case_id] = {
                        "case_id": case_id,
                        "status": semantic_status,
                        "reason": "local semantic grader stability verdict: %s" % semantic_status,
                        "evidence_refs": heuristic.get(case_id, {}).get("evidence_refs", ()),
                    }
        aggregates = build_case_aggregates(
            cases,
            results,
            evidence,
            verification_evidence,
            verification_results=verification_results,
            required_k=self.required_k,
            run_context={
                "task_id": task_id,
                "iteration": iteration,
                "primary_purpose": primary_batch.get("purpose", "evaluation"),
                "primary_created_at": primary_batch.get("created_at"),
                "verification_created_at": verification_batch.get("created_at") if verification_batch else None,
            },
        )

        # Stability is a property of the aggregate, never of a model's
        # ``verification_status`` field. This keeps missing/uncited evidence
        # from appearing as a stable pass in the UI or in a mutation decision.
        aggregate_by_id = {item.case_id: item for item in aggregates}
        verification_ids = {
            str(item.get("case_id"))
            for item in verification_evidence
            if isinstance(item, Mapping) and item.get("case_id")
        }
        if verification_batch is not None:
            stable_passes = [
                case_id
                for case_id in primary_passes
                if case_id in verification_ids
                and aggregate_by_id.get(case_id) is not None
                and aggregate_by_id[case_id].stable_pass
            ]
            flaky = [
                case_id
                for case_id in primary_passes
                if case_id in verification_ids
                and aggregate_by_id.get(case_id) is not None
                and aggregate_by_id[case_id].flaky
            ]
        else:
            stable_passes = []
            flaky = []
        failed = [item["case_id"] for item in results if item["status"] == "fail"] + flaky
        incremental_value_case_ids = [
            item["case_id"]
            for item in results
            if item["status"] == "pass"
            and baseline_heuristic.get(item["case_id"], {}).get("status") == "fail"
        ]
        eligible_flaky = [item for item in flaky if item in optimization_eligible_case_ids]
        if eligible_flaky:
            failure_clusters = list(failure_clusters) + [
                {
                    "id": "unstable-pass-verification",
                    "case_ids": list(eligible_flaky),
                    "root_cause": "a trusted primary pass did not reproduce in the required verification run",
                    "skill_change_authorized": True,
                }
            ]
            proposed_changes = list(proposed_changes) + [
                {
                    "target": entrypoint,
                    "change": "make the affected workflow deterministic and preserve its required evidence",
                    "why": "a trusted primary pass was not stable on verification",
                    "case_ids": list(eligible_flaky),
                }
            ]
            if entrypoint not in target_scope:
                target_scope = list(target_scope) + [entrypoint]
        aggregate_not_evaluable = [
            item.case_id for item in aggregates if item.status == "not_evaluable"
        ]
        not_evaluable = sorted(set(
            [item["case_id"] for item in results if item["status"] == "not_evaluable"]
            + aggregate_not_evaluable
        ))
        evidence_issues = [
            _evidence_issue(case_id, heuristic.get(case_id, {}), aggregate_by_id.get(case_id))
            for case_id in not_evaluable
        ]
        if primary_passes and verification_batch is None:
            next_action = "verify_passes"
        elif not_evaluable:
            next_action = "needs_evidence"
        elif failed:
            next_action = "await_user_confirmation"
        else:
            next_action = "converged"
        intervention_blocker = None
        if next_action == "await_user_confirmation":
            authorized = any(
                isinstance(cluster, Mapping) and cluster.get("skill_change_authorized") is True
                for cluster in failure_clusters
            )
            supported_scope = set(str(item) for item in (editable_resource_inventory or (entrypoint,)))
            unsupported_scope = [str(item) for item in target_scope if not isinstance(item, str) or item not in supported_scope]
            if not authorized:
                next_action = "needs_evidence"
                intervention_blocker = (
                    "unstable Cases are not Oracle-ready and cannot authorize a Skill change"
                    if flaky and not eligible_flaky
                    else "no trusted failed Case authorizes a Skill change"
                )
            elif unsupported_scope:
                next_action = "needs_evidence"
                intervention_blocker = "target scope is not an editable existing Skill resource: %s" % ", ".join(unsupported_scope)

        diagnosis_graph = compile_diagnosis_graph(results, failure_clusters, proposed_changes, conflicts)
        result = {
            "api_version": ANALYSIS_DECISION_API_VERSION,
            "task_id": task_id,
            "iteration": iteration,
            "primary_purpose": str(primary_batch.get("purpose") or "evaluation"),
            "case_results": results,
            "stable_pass_case_ids": stable_passes,
            "verification_required_case_ids": primary_passes if verification_batch is None else [],
            "flaky_case_ids": flaky,
            "failed_case_ids": failed,
            "not_evaluable_case_ids": not_evaluable,
            "evidence_issues": evidence_issues,
            "recovery": {
                "full_restart_required": False,
                "targeted_retry_case_ids": [
                    item["case_id"] for item in evidence_issues
                    if item.get("targeted_retry_available")
                ],
                "reopen_case_review_case_ids": [
                    item["case_id"] for item in evidence_issues
                    if item.get("recommended_action") == "reopen_case_review"
                ],
            },
            "without_skill_baseline_case_results": [baseline_heuristic.get(str(case.get("id")), {"case_id": str(case.get("id")), "status": "not_evaluable", "reason": "baseline requires semantic analysis", "evidence_refs": []}) for case in cases] if comparison_baseline_batch is not None else [],
            "incremental_value_case_ids": incremental_value_case_ids,
            "failure_clusters": failure_clusters,
            "conflicts": conflicts,
            "proposed_changes": proposed_changes,
            "target_scope": target_scope,
            "optimization_eligible_case_ids": sorted(optimization_eligible_case_ids),
            "next_action": next_action,
            "intervention_blocker": intervention_blocker,
            "requires_user_confirmation": next_action == "await_user_confirmation",
            "analysis_usage": model_usage,
            "attempt_verdicts": [
                attempt.to_dict()
                for aggregate in aggregates
                for attempt in aggregate.attempts
            ],
            "case_aggregates": [aggregate.to_dict() for aggregate in aggregates],
            "diagnosis_graph": diagnosis_graph,
            "evidence_health": {
                "valid_attempts": sum(
                    attempt.evidence_validity.status == "valid"
                    for aggregate in aggregates
                    for attempt in aggregate.attempts
                ),
                "invalid_attempts": sum(
                    attempt.evidence_validity.status == "invalid"
                    for aggregate in aggregates
                    for attempt in aggregate.attempts
                ),
                "not_evaluable_case_ids": [aggregate.case_id for aggregate in aggregates if aggregate.status == "not_evaluable"],
            },
            "token_economy": {
                "model_calls": 1 if model_called else 0,
                "full_logs_sent": False,
                "evidence_artifacts": [item.get("artifact") for item in evidence],
                "serialized_prompt_chars": serialized_prompt_chars,
                "estimated_prompt_tokens": (serialized_prompt_chars + 3) // 4,
            },
        }
        return result


__all__ = [
    "CrossCaseAnalyzer",
    "EvidenceAnalysisError",
    "compact_case_evidence",
    "normalize_skill_path",
    "skill_entrypoint_path",
]
