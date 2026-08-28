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
    for index, event in enumerate(trace):
        if event.get("error"):
            errors.append({"index": index, "error": str(event.get("error"))[:800]})
        kind = str(event.get("kind") or event.get("type") or "")
        if kind not in ("tool_result", "tool_call") or remaining <= 0:
            continue
        text = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        clipped = text[: min(900, remaining)]
        evidence.append({"index": index, "text": clipped})
        remaining -= len(clipped)
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
            "run_status": row.get("status"),
            "terminal": row.get("terminal"),
            "error": row.get("error") or "remote attempt did not produce an immutable session artifact",
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
        "run_status": row.get("status"),
        "terminal": row.get("terminal"),
        "error": observation.get("error") or row.get("error"),
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
    if evidence.get("run_status") != "completed" or evidence.get("error"):
        return {"case_id": case.get("id"), "status": "not_evaluable", "reason": str(evidence.get("error") or "remote run failed; infrastructure and Skill responsibility are not distinguishable"), "evidence_refs": [evidence.get("artifact")]}
    log_completeness = evidence.get("log_completeness")
    if isinstance(log_completeness, Mapping) and log_completeness.get("complete") is False:
        reasons = list(log_completeness.get("reason_codes", ()))
        missing = list(log_completeness.get("missing_required_event_types", ()))
        detail = ", ".join(str(item) for item in reasons + missing) or "complete session log was not proven"
        return {"case_id": case.get("id"), "status": "not_evaluable", "reason": "session evidence is incomplete: %s" % detail, "evidence_refs": [evidence.get("artifact")]}
    path = evidence.get("path_conformance")
    if isinstance(path, Mapping) and path.get("status") == "not_evaluable":
        return {"case_id": case.get("id"), "status": "not_evaluable", "reason": str(path.get("reason")), "evidence_refs": [evidence.get("artifact")]}
    if isinstance(path, Mapping) and path.get("status") == "fail":
        return {"case_id": case.get("id"), "status": "fail", "reason": "execution path violates the Skill contract", "evidence_refs": [evidence.get("artifact")]}
    # Path conformance is a hard contract. A deterministic outcome Grader may
    # add dimensions, but it must never turn a path violation into a passing
    # Case or hide an incomplete trace from mutation authorization.
    formal = evidence.get("formal_grading")
    if isinstance(formal, Mapping):
        formal_status = str(formal.get("status") or "not_evaluable")
        if formal_status in ("pass", "fail", "not_evaluable"):
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
    return None


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
                    "editable_resource_inventory": list(editable_resource_inventory or ("SKILL.md",)),
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
                # Exact user expectations and deterministic execution-path
                # failures are authoritative. The model may explain them but
                # must never reverse them.
                heuristic.update(deterministic_results)
                model_baseline = {
                    str(item["case_id"]): dict(item)
                    for item in decision.get("without_skill_baseline_case_results", ())
                }
                model_baseline.update(baseline_heuristic)
                baseline_heuristic = model_baseline
                failure_clusters = list(decision["failure_clusters"])
                conflicts = list(decision["conflicts"])
                proposed_changes = list(decision["proposed_changes"])
                target_scope = list(decision["target_scope"])
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
            proposed_changes = ([{"target": "SKILL.md", "change": "repair the shared cause supported by failed case evidence", "why": "one or more frozen expectations failed", "case_ids": failed_ids}] if failed_ids else [])
            target_scope = ["SKILL.md"] if failed_ids else []

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
                    "target": "SKILL.md",
                    "change": "make the affected workflow deterministic and preserve its required evidence",
                    "why": "a trusted primary pass was not stable on verification",
                    "case_ids": list(eligible_flaky),
                }
            ]
            if "SKILL.md" not in target_scope:
                target_scope = list(target_scope) + ["SKILL.md"]
        aggregate_not_evaluable = [
            item.case_id for item in aggregates if item.status == "not_evaluable"
        ]
        not_evaluable = sorted(set(
            [item["case_id"] for item in results if item["status"] == "not_evaluable"]
            + aggregate_not_evaluable
        ))
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
            supported_scope = set(str(item) for item in (editable_resource_inventory or ("SKILL.md",)))
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
            "case_results": results,
            "stable_pass_case_ids": stable_passes,
            "verification_required_case_ids": primary_passes if verification_batch is None else [],
            "flaky_case_ids": flaky,
            "failed_case_ids": failed,
            "not_evaluable_case_ids": not_evaluable,
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


__all__ = ["CrossCaseAnalyzer", "EvidenceAnalysisError", "compact_case_evidence"]
