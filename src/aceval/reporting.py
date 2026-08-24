"""Deterministic JSON and Markdown reports for EvalOps results."""

from __future__ import annotations

import hashlib
import html
import json
import math
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Dict, Iterable, List, Mapping, MutableSet, Optional, Sequence, Tuple

from .contracts import GradeStatus, is_tool_call_event
from .orchestrator import (
    CandidateTrial,
    ComparisonResult,
    EvalRun,
    OptimizationResult,
    ValidationAttempt,
)


REPORT_SCHEMA_VERSION = "aceval.report/v1"
_BYTE_PREVIEW_SIZE = 32


def to_report_dict(value: Any, detailed: bool = False) -> Dict[str, Any]:
    """Return a JSON-safe, deterministic representation of an EvalOps result."""

    serialized = _json_safe(value)
    if not detailed:
        serialized = _summary_payload(serialized)
    payload = dict(serialized) if isinstance(serialized, Mapping) else {"value": serialized}
    payload["schema_version"] = REPORT_SCHEMA_VERSION
    payload["detail_level"] = "detailed" if detailed else "summary"
    payload["simulated"] = _is_simulated(value)

    if isinstance(value, EvalRun):
        payload["report_type"] = "eval_run"
        payload["summary"] = _run_summary(value)
        payload["limitations"] = list(_limitations(value))
    elif isinstance(value, ComparisonResult):
        payload["report_type"] = "comparison"
        payload["accepted"] = value.accepted
        payload["status"] = value.status
        payload["evaluable"] = value.evaluable
        payload["hard_regression_count"] = len(value.hard_regressions)
        payload["summary"] = _comparison_summary(value)
        payload["limitations"] = list(_limitations(value))
    elif isinstance(value, OptimizationResult):
        payload["report_type"] = "optimization"
        payload["summary"] = _optimization_summary(value)
        payload["limitations"] = list(_limitations(value))
    else:
        payload["report_type"] = _python_type(value)
    return payload


def write_report(
    value: Any,
    output_dir: Path,
    basename: str = "report",
    detailed: bool = False,
) -> Tuple[Path, Path]:
    """Write deterministic ``<basename>.json`` and ``<basename>.md`` files."""

    _validate_basename(basename)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / (basename + ".json")
    markdown_path = destination / (basename + ".md")
    report = to_report_dict(value, detailed=detailed)
    json_path.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(value, report), encoding="utf-8")
    return json_path, markdown_path


def _json_safe(value: Any, stack: Optional[MutableSet[int]] = None) -> Any:
    if isinstance(value, Enum):
        return _json_safe(value.value, stack)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"$type": "float", "value": str(value).lower()}
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        content = bytes(value)
        preview = content[:_BYTE_PREVIEW_SIZE]
        return {
            "$type": "bytes",
            "content_omitted": True,
            "omitted": True,
            "preview": preview.hex(),
            "preview_encoding": "hex",
            "preview_truncated": len(content) > len(preview),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, BaseException):
        return {
            "$type": "exception",
            "exception_type": _python_type(value),
            "message": str(value),
        }

    active = stack if stack is not None else set()
    tracked = is_dataclass(value) or isinstance(
        value, (Mapping, list, tuple, set, frozenset)
    )
    identity = id(value)
    if tracked and identity in active:
        return {"$type": "cycle", "python_type": _python_type(value)}
    if tracked:
        active.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            return {
                item.name: _json_safe(getattr(value, item.name), active)
                for item in fields(value)
            }
        if isinstance(value, Mapping):
            items = [(_mapping_key(key), _json_safe(item, active)) for key, item in value.items()]
            return {key: item for key, item in sorted(items, key=lambda pair: pair[0])}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item, active) for item in value]
        if isinstance(value, (set, frozenset)):
            items = [_json_safe(item, active) for item in value]
            return sorted(items, key=_canonical_json)
    finally:
        if tracked:
            active.remove(identity)

    return {"$type": "unsupported", "python_type": _python_type(value)}


def _summary_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        summary = {}
        for key, item in value.items():
            if key == "observation":
                continue
            if key == "evidence" and isinstance(item, list):
                summary["evidence_count"] = len(item)
                continue
            if key == "patch" and isinstance(item, str):
                encoded = item.encode("utf-8")
                summary["patch_omitted"] = True
                summary["patch_sha256"] = hashlib.sha256(encoded).hexdigest()
                summary["patch_size"] = len(encoded)
                continue
            summary[key] = _summary_payload(item)
        return summary
    if isinstance(value, list):
        return [_summary_payload(item) for item in value]
    return value


def _mapping_key(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, PurePath):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return str(value)
    return _canonical_json(_json_safe(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _python_type(value: Any) -> str:
    value_type = value if isinstance(value, type) else type(value)
    return "%s.%s" % (value_type.__module__, value_type.__qualname__)


def _validate_basename(basename: str) -> None:
    if not isinstance(basename, str) or not basename or basename in (".", ".."):
        raise ValueError("basename must be a non-empty file stem")
    if Path(basename).name != basename or "/" in basename or "\\" in basename:
        raise ValueError("basename must not contain a path")


def _ordered_splits(run: EvalRun) -> Tuple[str, ...]:
    declared = list(run.requested_splits)
    extras = sorted({item.split for item in run.scenarios}.difference(declared))
    return tuple(dict.fromkeys(declared + extras))


def _split_summary(run: EvalRun, split: str) -> Dict[str, Any]:
    scenarios = tuple(item for item in run.scenarios if item.split == split)
    pass_count = sum(item.passed for item in scenarios)
    hard_pass_count = sum(item.hard_passed for item in scenarios)
    total = len(scenarios)
    status_counts = {}
    for item in scenarios:
        key = item.status.value
        status_counts[key] = status_counts.get(key, 0) + 1
    return {
        "split": split,
        "passed": bool(scenarios) and hard_pass_count == total,
        "pass_count": pass_count,
        "hard_pass_count": hard_pass_count,
        "total_count": total,
        "pass_rate": pass_count / total if total else 0.0,
        "hard_pass_rate": hard_pass_count / total if total else 0.0,
        "status_counts": status_counts,
    }


def _measurement_summary(run: EvalRun) -> Dict[str, Any]:
    usages = []
    for scenario in run.scenarios:
        if scenario.observation is not None and scenario.observation.usage:
            usages.append((scenario.scenario_id, scenario.observation.usage))

    input_tokens = []  # type: List[float]
    output_tokens = []  # type: List[float]
    total_tokens = []  # type: List[float]
    costs = []  # type: List[float]
    token_scenarios = 0
    cost_scenarios = 0
    tool_calls = 0
    trace_scenarios = 0
    alias_conflicts = {}
    for _, usage in usages:
        input_value = _max_number(usage, ("input_tokens", "prompt_tokens"))
        output_value = _max_number(usage, ("output_tokens", "completion_tokens"))
        total_value = _first_number(usage, ("total_tokens",))
        component_total = (
            input_value + output_value
            if input_value is not None and output_value is not None
            else None
        )
        if total_value is None:
            total_value = component_total
        elif component_total is not None:
            total_value = max(total_value, component_total)
        cost_value = _max_number(usage, ("cost_usd", "total_cost_usd"))
        if input_value is not None:
            input_tokens.append(input_value)
        if output_value is not None:
            output_tokens.append(output_value)
        if total_value is not None:
            total_tokens.append(total_value)
        if (
            input_value is not None
            or output_value is not None
            or total_value is not None
        ):
            token_scenarios += 1
        if cost_value is not None:
            costs.append(cost_value)
            cost_scenarios += 1
    for scenario in run.scenarios:
        if scenario.observation is not None:
            trace_scenarios += 1
            tool_calls += sum(
                is_tool_call_event(event) for event in scenario.observation.trace
            )
    for scenario_id, usage in usages:
        conflicts = _usage_alias_conflicts(usage)
        if conflicts:
            alias_conflicts[scenario_id] = conflicts

    total_scenarios = len(run.scenarios)

    def measurement_status(count: int) -> str:
        if count == 0:
            return "not_measured"
        if count < total_scenarios:
            return "partial"
        return "measured"

    return {
        "usage": (
            {
                scenario_id: _json_safe(dict(usage))
                for scenario_id, usage in usages
            }
            if usages
            else None
        ),
        "input_tokens": _sum_or_none(input_tokens),
        "output_tokens": _sum_or_none(output_tokens),
        "total_tokens": _sum_or_none(total_tokens),
        "cost_usd": _sum_or_none(costs),
        "tool_calls": tool_calls if trace_scenarios else None,
        "usage_alias_conflicts": alias_conflicts or None,
        "flake_rate": None,
        "measured_scenarios": {
            "usage": len(usages),
            "tokens": token_scenarios,
            "cost": cost_scenarios,
            "total": total_scenarios,
        },
        "measurement_status": {
            "usage": measurement_status(len(usages)),
            "tokens": measurement_status(token_scenarios),
            "cost": measurement_status(cost_scenarios),
            "tool_calls": measurement_status(trace_scenarios),
            "flake_rate": "not_measured",
        },
    }


def _first_number(usage: Mapping[str, Any], keys: Sequence[str]) -> Optional[float]:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if math.isfinite(float(value)):
                    return value
            except OverflowError:
                continue
    return None


def _max_number(usage: Mapping[str, Any], keys: Sequence[str]) -> Optional[float]:
    values = []
    for key in keys:
        value = _first_number(usage, (key,))
        if value is not None:
            values.append(value)
    return max(values) if values else None


def _usage_alias_conflicts(usage: Mapping[str, Any]) -> List[str]:
    conflicts = []
    for left, right in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("cost_usd", "total_cost_usd"),
    ):
        left_value = _first_number(usage, (left,))
        right_value = _first_number(usage, (right,))
        if (
            left_value is not None
            and right_value is not None
            and left_value != right_value
        ):
            conflicts.append("%s!=%s" % (left, right))
    total = _first_number(usage, ("total_tokens",))
    input_value = _max_number(usage, ("input_tokens", "prompt_tokens"))
    output_value = _max_number(usage, ("output_tokens", "completion_tokens"))
    if (
        total is not None
        and input_value is not None
        and output_value is not None
        and total < input_value + output_value
    ):
        conflicts.append("total_tokens<component_sum")
    return conflicts


def _sum_or_none(values: Sequence[float]) -> Optional[float]:
    return sum(values) if values else None


def _run_summary(run: EvalRun) -> Dict[str, Any]:
    summary = {
        "pack": run.pack_name,
        "runtime": run.runtime_id,
        "model": (
            run.runtime_profile.model if run.runtime_profile is not None else None
        ),
        "runtime_profile_hash": (
            run.runtime_profile.profile_hash
            if run.runtime_profile is not None
            else None
        ),
        "subject": run.subject_uri,
        "subject_variant": run.subject_variant,
        "simulated": run.simulated or run.runtime_id == "fake",
        "passed": run.passed,
        "pass_count": run.pass_count,
        "hard_pass_count": run.hard_pass_count,
        "total_count": run.total_count,
        "pass_rate": run.pass_rate,
        "hard_pass_rate": run.hard_pass_rate,
        "duration_seconds": run.duration_seconds,
        "splits": [_split_summary(run, split) for split in _ordered_splits(run)],
    }
    summary.update(_measurement_summary(run))
    summary["diagnostics"] = _diagnostic_summary(run)
    return summary


def _diagnostic_summary(run: EvalRun) -> Mapping[str, Any]:
    cards = []
    decisions = {}
    for scenario in run.scenarios:
        report = getattr(scenario, "diagnostic_report", None)
        if report is None:
            continue
        decision = report.patch_decision.value
        decisions[decision] = decisions.get(decision, 0) + 1
        cards.extend(report.failure_cards)
    categories = {}
    components = {}
    for card in cards:
        categories[card.category] = categories.get(card.category, 0) + 1
        components[card.observed_component] = (
            components.get(card.observed_component, 0) + 1
        )
    return {
        "scenario_decisions": decisions,
        "failure_card_count": len(cards),
        "patchable_card_count": sum(card.skill_patch_authorized for card in cards),
        "blocked_card_count": sum(
            not card.skill_patch_authorized for card in cards
        ),
        "categories": categories,
        "observed_components": components,
    }


def _paired_summary(baseline: EvalRun, candidate: EvalRun) -> Dict[str, Any]:
    baseline_items = {item.scenario_id: item for item in baseline.scenarios}
    candidate_items = {item.scenario_id: item for item in candidate.scenarios}
    shared = sorted(set(baseline_items).intersection(candidate_items))
    evaluable = all(
        item.error is None
        and item.status in (GradeStatus.PASS, GradeStatus.FAIL)
        for run in (baseline, candidate)
        for item in run.scenarios
    ) and bool(baseline.scenarios and candidate.scenarios)
    improvements = [
        scenario_id
        for scenario_id in shared
        if evaluable
        and not baseline_items[scenario_id].hard_passed
        and candidate_items[scenario_id].hard_passed
    ]
    regressions = [
        scenario_id
        for scenario_id in shared
        if evaluable
        and baseline_items[scenario_id].hard_passed
        and not candidate_items[scenario_id].hard_passed
    ]
    return {
        "evaluable": evaluable,
        "paired_uplift": (
            candidate.hard_pass_rate - baseline.hard_pass_rate
            if evaluable
            else None
        ),
        "improvements": improvements,
        "hard_regressions": regressions,
        "hard_regression_count": len(regressions),
    }


def _comparison_summary(value: ComparisonResult) -> Dict[str, Any]:
    return {
        "simulated": _is_simulated(value),
        "accepted": value.accepted,
        "status": value.status,
        "evaluable": value.evaluable,
        "paired_uplift": value.paired_uplift,
        "improvements": list(value.improvements),
        "hard_regressions": list(value.hard_regressions),
        "hard_regression_count": len(value.hard_regressions),
        "baseline": _run_summary(value.baseline),
        "candidate": _run_summary(value.candidate),
    }


def _selected_trial(value: OptimizationResult) -> Optional[CandidateTrial]:
    if value.selected_candidate_id is None:
        return None
    return next(
        (
            trial
            for trial in value.trials
            if trial.candidate_id == value.selected_candidate_id
        ),
        None,
    )


def _gate_summary(
    name: str, baseline: Optional[EvalRun], candidate: Optional[EvalRun]
) -> Dict[str, Any]:
    if candidate is None:
        gate = {"gate": name, "status": "not_run"}
        if baseline is not None:
            baseline_status = _gate_run_status(baseline)
            gate.update(
                {
                    "baseline_status": baseline_status,
                    "baseline_hard_pass_rate": baseline.hard_pass_rate,
                    "candidate_status": "not_run",
                }
            )
            if baseline_status in ("error", "not_evaluable", "fail"):
                gate["status"] = baseline_status
        return gate
    status = _gate_run_status(candidate)
    gate = {
        "gate": name,
        "status": status,
        "candidate_hard_pass_rate": candidate.hard_pass_rate,
    }
    if baseline is not None:
        paired = _paired_summary(baseline, candidate)
        gate.update(paired)
        if paired["hard_regressions"] and gate["status"] == "pass":
            gate["status"] = "fail"
        gate["baseline_hard_pass_rate"] = baseline.hard_pass_rate
        gate["baseline_status"] = _gate_run_status(baseline)
        if gate["baseline_status"] in ("error", "not_evaluable"):
            gate["status"] = gate["baseline_status"]
    return gate


def _gate_run_status(run: EvalRun) -> str:
    statuses = {item.status for item in run.scenarios}
    if GradeStatus.ERROR in statuses or any(item.error for item in run.scenarios):
        return "error"
    elif GradeStatus.NOT_EVALUABLE in statuses:
        return "not_evaluable"
    return "pass" if run.passed else "fail"


def _objective_summary(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    return {
        "objective_id": value.objective_id,
        "evaluable": value.evaluable,
        "passed": value.passed,
        "baseline": {
            "value": value.baseline.value,
            "coverage": value.baseline.coverage,
            "status": value.baseline.status,
            "scenario_values": dict(value.baseline.scenario_values),
        },
        "candidate": {
            "value": value.candidate.value,
            "coverage": value.candidate.coverage,
            "status": value.candidate.status,
            "scenario_values": dict(value.candidate.scenario_values),
        },
        "raw_delta": value.raw_delta,
        "improvement": value.improvement,
        "case_regressions": list(value.case_regressions),
        "meets_min_delta": value.meets_min_delta,
        "meets_target": value.meets_target,
    }


def _validation_attempt_summary(
    value: OptimizationResult, attempt: ValidationAttempt
) -> Dict[str, Any]:
    gate = _gate_summary("validation", value.baseline_validation, attempt.run)
    objective = _objective_summary(attempt.objective_comparison)
    status = gate["status"]
    if objective is not None and not objective["passed"] and status == "pass":
        status = "fail"
    return {
        "candidate_id": attempt.candidate_id,
        "candidate_hash": attempt.candidate_hash,
        "status": status,
        "passed_gate": attempt.passed_gate,
        "hard_regressions": list(attempt.hard_regressions),
        "run_id": attempt.run.run_id,
        "objective": objective,
    }


def _optimization_summary(value: OptimizationResult) -> Dict[str, Any]:
    selected = _selected_trial(value)
    selected_dev = selected.dev_run if selected is not None else None
    displayed_validation_objective = value.validation_objective
    if displayed_validation_objective is None and value.candidate_validation is not None:
        displayed_validation_objective = next(
            (
                attempt.objective_comparison
                for attempt in value.validation_attempts
                if attempt.run.run_id == value.candidate_validation.run_id
            ),
            None,
        )
    holdout_gate = _gate_summary(
        "holdout", value.baseline_holdout, value.candidate_holdout
    )
    if value.baseline_holdout is None:
        holdout_gate.update(
            {
                "baseline_status": "not_measured",
                "paired_metrics_status": "not_measured",
            }
        )
    if value.holdout_objective is not None:
        holdout_gate["objective"] = _objective_summary(value.holdout_objective)
        if not value.holdout_objective.passed and holdout_gate["status"] == "pass":
            holdout_gate["status"] = "fail"
    dev_gate = _gate_summary("dev", value.baseline_dev, selected_dev)
    validation_gate = _gate_summary(
        "validation", value.baseline_validation, value.candidate_validation
    )
    if value.dev_objective is not None:
        dev_gate["objective"] = _objective_summary(value.dev_objective)
        if not value.dev_objective.passed and dev_gate["status"] == "pass":
            dev_gate["status"] = "fail"
    if displayed_validation_objective is not None:
        validation_gate["objective"] = _objective_summary(
            displayed_validation_objective
        )
        if (
            not displayed_validation_objective.passed
            and validation_gate["status"] == "pass"
        ):
            validation_gate["status"] = "fail"
    return {
        "simulated": _is_simulated(value),
        "accepted": value.accepted,
        "stop_reason": value.stop_reason,
        "mode": value.mode,
        "goal": value.goal,
        "objective": _json_safe(value.objective),
        "eval_suite_hash": value.eval_suite_hash,
        "experiment_plan_hash": value.experiment_plan_hash,
        "experiment_plan_source": value.experiment_plan_source,
        "dev_objective": _objective_summary(value.dev_objective),
        "validation_objective": _objective_summary(displayed_validation_objective),
        "holdout_objective": _objective_summary(value.holdout_objective),
        "selected_candidate_id": value.selected_candidate_id,
        "selected_candidate_path": (
            str(value.selected_candidate_path)
            if value.selected_candidate_path is not None
            else None
        ),
        "selected_candidate_hash": value.selected_candidate_hash,
        "trial_count": len(value.trials),
        "promoted_trial_count": sum(item.promoted_from_dev for item in value.trials),
        "validation_attempt_count": len(value.validation_attempts),
        "proposal_attempt_count": value.proposal_attempt_count,
        "rejected_proposal_count": value.rejected_proposal_count,
        "duplicate_proposal_count": value.duplicate_proposal_count,
        "experiment_usage": _json_safe(value.experiment_usage),
        "validation_attempts": [
            _validation_attempt_summary(value, attempt)
            for attempt in value.validation_attempts
        ],
        "holdout_batch_count": value.holdout_batch_count,
        "holdout_pair_count": value.holdout_pair_count,
        "hidden_regression_rate": None,
        "measurement_status": {
            "baseline_holdout": (
                (
                    "attempted_not_evaluable"
                    if _gate_run_status(value.baseline_holdout)
                    in ("error", "not_evaluable")
                    else "measured"
                )
                if value.baseline_holdout is not None
                else "not_measured"
            ),
            "candidate_holdout": (
                (
                    "attempted_not_evaluable"
                    if _gate_run_status(value.candidate_holdout)
                    in ("error", "not_evaluable")
                    else "measured"
                )
                if value.candidate_holdout is not None
                else "not_run"
            ),
            "hidden_regression_rate": "not_measured",
        },
        "baseline": _run_summary(value.baseline_dev),
        "gates": [
            dev_gate,
            validation_gate,
            holdout_gate,
        ],
    }


def _limitations(value: Any) -> Tuple[str, ...]:
    values = []  # type: List[str]

    def add(run: Optional[EvalRun]) -> None:
        if run is not None:
            values.extend(str(item) for item in run.limitations)

    if isinstance(value, EvalRun):
        add(value)
    elif isinstance(value, ComparisonResult):
        add(value.baseline)
        add(value.candidate)
    elif isinstance(value, OptimizationResult):
        values.extend(str(item) for item in value.limitations)
        add(value.baseline_dev)
        add(value.baseline_validation)
        add(value.candidate_validation)
        add(value.baseline_holdout)
        add(value.candidate_holdout)
        for trial in value.trials:
            add(trial.dev_run)
        for attempt in value.validation_attempts:
            add(attempt.run)
    return tuple(dict.fromkeys(values))


def _is_simulated(value: Any) -> bool:
    if isinstance(value, EvalRun):
        return value.simulated or value.runtime_id == "fake"
    if isinstance(value, ComparisonResult):
        return _is_simulated(value.baseline) or _is_simulated(value.candidate)
    if isinstance(value, OptimizationResult):
        runs = [
            value.baseline_dev,
            value.baseline_validation,
            value.candidate_validation,
            value.baseline_holdout,
            value.candidate_holdout,
        ]
        runs.extend(trial.dev_run for trial in value.trials)
        runs.extend(attempt.run for attempt in value.validation_attempts)
        return any(run is not None and _is_simulated(run) for run in runs)
    return False


def _render_markdown(value: Any, report: Mapping[str, Any]) -> str:
    if isinstance(value, EvalRun):
        return _render_run(value)
    if isinstance(value, ComparisonResult):
        return _render_comparison(value)
    if isinstance(value, OptimizationResult):
        return _render_optimization(value)
    return "# EvalOps Report\n\n```json\n%s\n```\n" % json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    )


def _render_run(run: EvalRun) -> str:
    summary = _run_summary(run)
    lines = ["# Evaluation Report", ""]
    _append_simulated_banner(lines, _is_simulated(run))
    lines.extend(
        [
        _table(
            ("Field", "Value"),
            (
                ("Pack", run.pack_name),
                ("Runtime", run.runtime_id),
                (
                    "Model",
                    run.runtime_profile.model
                    if run.runtime_profile is not None
                    and run.runtime_profile.model is not None
                    else "not declared",
                ),
                (
                    "Runtime profile",
                    run.runtime_profile.profile_hash
                    if run.runtime_profile is not None
                    else "not captured",
                ),
                ("Subject", run.subject_uri),
                ("Variant", run.subject_variant),
                ("Overall", _pass_label(run.passed)),
                ("Hard pass rate", _percent(run.hard_pass_rate)),
            ),
        ),
        "",
        "## Split Results",
        "",
        _run_split_table(run),
        "",
        "## Scenario Results",
        "",
        _table(
            ("Split", "Scenario", "Status", "Hard gate", "Error"),
            tuple(
                (
                    item.split,
                    item.scenario_id,
                    item.status.value,
                    _pass_label(item.hard_passed),
                    item.error or "—",
                )
                for item in run.scenarios
            ),
        ),
        "",
        "## Measurements",
        "",
        _measurement_table(summary),
        "",
        "## Failure Attribution",
        "",
        _table(
            ("Scenario", "Patch decision", "Failure cards", "Blocked reason"),
            tuple(
                (
                    item.scenario_id,
                    (
                        item.diagnostic_report.patch_decision.value
                        if item.diagnostic_report is not None
                        else "not_run"
                    ),
                    (
                        len(item.diagnostic_report.failure_cards)
                        if item.diagnostic_report is not None
                        else 0
                    ),
                    (
                        _joined(item.diagnostic_report.blocked_reasons)
                        if item.diagnostic_report is not None
                        else "—"
                    ),
                )
                for item in run.scenarios
            ),
        ),
        ]
    )
    _append_limitations(lines, _limitations(run))
    return "\n".join(lines).rstrip() + "\n"


def _render_comparison(value: ComparisonResult) -> str:
    baseline = value.baseline
    candidate = value.candidate
    split_names = tuple(
        dict.fromkeys(_ordered_splits(baseline) + _ordered_splits(candidate))
    )
    split_rows = []
    for split in split_names:
        old = _split_summary(baseline, split)
        new = _split_summary(candidate, split)
        if old["status_counts"].get("error") or new["status_counts"].get("error"):
            split_gate = "error"
        elif old["status_counts"].get("not_evaluable") or new["status_counts"].get(
            "not_evaluable"
        ):
            split_gate = "not_evaluable"
        else:
            split_gate = "pass" if new["passed"] else "fail"
        split_uplift = (
            new["hard_pass_rate"] - old["hard_pass_rate"]
            if split_gate in ("pass", "fail")
            else None
        )
        split_rows.append(
            (
                split,
                _fraction(old),
                _fraction(new),
                _pp_or_dash(split_uplift),
                split_gate,
            )
        )
    baseline_summary = _run_summary(baseline)
    candidate_summary = _run_summary(candidate)
    lines = ["# Comparison Report", ""]
    _append_simulated_banner(lines, _is_simulated(value))
    lines.extend(
        [
        _table(
            ("Field", "Value"),
            (
                ("Pack", _pair_text(baseline.pack_name, candidate.pack_name)),
                ("Runtime", _pair_text(baseline.runtime_id, candidate.runtime_id)),
                ("Subject", "%s → %s" % (baseline.subject_uri, candidate.subject_uri)),
                ("Decision", value.status),
                ("Accepted", _pass_label(value.accepted)),
            ),
        ),
        "",
        "## Split Results",
        "",
        _table(
            ("Split", "Baseline hard pass", "Candidate hard pass", "Uplift", "Gate"),
            tuple(split_rows),
        ),
        "",
        "## Paired Decision",
        "",
        _table(
            ("Metric", "Value"),
            (
                ("Paired uplift", _pp_or_dash(value.paired_uplift)),
                ("Improvements", _joined(value.improvements)),
                ("Hard regressions", _joined(value.hard_regressions)),
            ),
        ),
        "",
        "## Measurements",
        "",
        _comparison_measurement_table(baseline_summary, candidate_summary),
        ]
    )
    _append_limitations(lines, _limitations(value))
    return "\n".join(lines).rstrip() + "\n"


def _render_optimization(value: OptimizationResult) -> str:
    baseline = value.baseline_dev
    summary = _optimization_summary(value)
    gates = summary["gates"]
    gate_rows = []
    for gate in gates:
        objective = gate.get("objective") or {}
        gate_rows.append(
            (
                gate["gate"],
                gate["status"],
                _percent_or_dash(gate.get("baseline_hard_pass_rate")),
                _percent_or_dash(gate.get("candidate_hard_pass_rate")),
                _pp_or_dash(gate.get("paired_uplift")),
                _delta_or_dash(objective.get("improvement")),
                _joined(gate.get("hard_regressions", ())),
            )
        )
    trial_rows = []
    for trial in value.trials:
        paired = _paired_summary(value.baseline_dev, trial.dev_run)
        trial_rows.append(
            (
                trial.round_index,
                trial.candidate_id,
                _percent(trial.dev_run.hard_pass_rate),
                _pp_or_dash(paired["paired_uplift"]),
                _delta_or_dash(
                    trial.objective_comparison.improvement
                    if trial.objective_comparison is not None
                    else None
                ),
                _joined(paired["hard_regressions"]),
                _pass_label(trial.promoted_from_dev),
                trial.rejection_reason or "—",
            )
        )
    selected = _selected_trial(value)
    lines = ["# Optimization Report", ""]
    _append_simulated_banner(lines, _is_simulated(value))
    lines.extend(
        [
        _table(
            ("Field", "Value"),
            (
                ("Pack", value.pack_name),
                ("EvalSuite hash", value.eval_suite_hash or "—"),
                ("ExperimentPlan hash", value.experiment_plan_hash or "—"),
                ("ExperimentPlan source", value.experiment_plan_source or "—"),
                ("Runtime", baseline.runtime_id),
                ("Subject", baseline.subject_uri),
                ("Mode", value.mode),
                ("Goal", value.goal or "—"),
                (
                    "Objective",
                    value.objective.id if value.objective is not None else "—",
                ),
                ("Accepted", _pass_label(value.accepted)),
                ("Selected candidate", value.selected_candidate_id or "—"),
                ("Candidate path", value.selected_candidate_path or "—"),
                ("Candidate hash", value.selected_candidate_hash or "—"),
                ("Holdout batches", value.holdout_batch_count),
                ("Holdout pairs", value.holdout_pair_count),
                ("Proposal attempts", value.proposal_attempt_count),
                ("Rejected proposals", value.rejected_proposal_count),
                ("Duplicate proposals", value.duplicate_proposal_count),
                (
                    "Experiment usage",
                    json.dumps(
                        _json_safe(value.experiment_usage),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
                ("Hidden regression rate", "not measured"),
                ("Stop reason", value.stop_reason or "—"),
            ),
        ),
        "",
        "## Candidate Gates",
        "",
        _table(
            (
                "Gate",
                "Status",
                "Baseline",
                "Candidate",
                "Paired uplift",
                "Objective improvement",
                "Regressions",
            ),
            tuple(gate_rows),
        ),
        "",
        "## Candidate Trials",
        "",
        _table(
            (
                "Round",
                "Candidate",
                "Dev hard pass",
                "Uplift",
                "Objective improvement",
                "Regressions",
                "Promoted",
                "Reason",
            ),
            tuple(trial_rows),
        ),
        "",
        "## Dev Measurements",
        "",
        _comparison_measurement_table(
            _run_summary(value.baseline_dev),
            _run_summary(selected.dev_run) if selected is not None else None,
        ),
        ]
    )
    _append_limitations(lines, _limitations(value))
    return "\n".join(lines).rstrip() + "\n"


def _run_split_table(run: EvalRun) -> str:
    rows = []
    for split in _ordered_splits(run):
        summary = _split_summary(run, split)
        rows.append(
            (
                split,
                _fraction(summary, "pass_count"),
                _fraction(summary),
                _percent(summary["hard_pass_rate"]),
                _pass_label(summary["passed"]),
            )
        )
    return _table(
        ("Split", "Task pass", "Hard pass", "Hard pass rate", "Gate"), tuple(rows)
    )


def _measurement_table(summary: Mapping[str, Any]) -> str:
    statuses = summary["measurement_status"]
    usage = summary.get("usage")
    usage_value = "%d scenario(s)" % len(usage) if usage else "null"
    return _table(
        ("Metric", "Value", "Status"),
        (
            ("Usage", usage_value, statuses["usage"]),
            (
                "Input tokens",
                _number_or_null(summary.get("input_tokens")),
                _value_status(summary.get("input_tokens"), statuses["tokens"]),
            ),
            (
                "Output tokens",
                _number_or_null(summary.get("output_tokens")),
                _value_status(summary.get("output_tokens"), statuses["tokens"]),
            ),
            (
                "Total tokens",
                _number_or_null(summary.get("total_tokens")),
                _value_status(summary.get("total_tokens"), statuses["tokens"]),
            ),
            (
                "Cost (USD)",
                _cost_or_null(summary.get("cost_usd")),
                _value_status(summary.get("cost_usd"), statuses["cost"]),
            ),
            (
                "Tool calls",
                _number_or_null(summary.get("tool_calls")),
                _value_status(
                    summary.get("tool_calls"), statuses["tool_calls"]
                ),
            ),
            (
                "Flake rate",
                _percent_or_null(summary.get("flake_rate")),
                statuses["flake_rate"],
            ),
        ),
    )


def _comparison_measurement_table(
    baseline: Mapping[str, Any], candidate: Optional[Mapping[str, Any]]
) -> str:
    rows = []
    for label, key, formatter in (
        ("Input tokens", "input_tokens", _number_or_null),
        ("Output tokens", "output_tokens", _number_or_null),
        ("Total tokens", "total_tokens", _number_or_null),
        ("Cost (USD)", "cost_usd", _cost_or_null),
        ("Tool calls", "tool_calls", _number_or_null),
        ("Flake rate", "flake_rate", _percent_or_null),
    ):
        rows.append(
            (
                label,
                _measurement_cell(baseline, key, formatter),
                (
                    _measurement_cell(candidate, key, formatter)
                    if candidate is not None
                    else "not run"
                ),
            )
        )
    return _table(("Metric", "Baseline", "Candidate"), tuple(rows))


def _measurement_cell(
    summary: Mapping[str, Any], key: str, formatter: Any
) -> str:
    value = summary.get(key)
    if value is None:
        return "null (not_measured)"
    return formatter(value)


def _value_status(value: Any, measured_status: str) -> str:
    return measured_status if value is not None else "not_measured"


def _number_or_null(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _cost_or_null(value: Any) -> str:
    return "null" if value is None else "%.6f" % float(value)


def _percent_or_null(value: Any) -> str:
    return "null" if value is None else _percent(float(value))


def _table(headers: Sequence[Any], rows: Sequence[Sequence[Any]]) -> str:
    lines = [
        "| %s |" % " | ".join(_cell(item) for item in headers),
        "| %s |" % " | ".join("---" for _ in headers),
    ]
    if rows:
        lines.extend(
            "| %s |" % " | ".join(_cell(item) for item in row) for row in rows
        )
    else:
        lines.append("| %s |" % " | ".join("—" for _ in headers))
    return "\n".join(lines)


def _cell(value: Any) -> str:
    escaped = html.escape(str(value), quote=True)
    return escaped.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _fraction(summary: Mapping[str, Any], key: str = "hard_pass_count") -> str:
    return "%s/%s" % (summary[key], summary["total_count"])


def _pass_label(value: bool) -> str:
    return "PASS" if value else "FAIL"


def _percent(value: float) -> str:
    return "%.1f%%" % (float(value) * 100.0)


def _percentage_points(value: float) -> str:
    return "%+.1f pp" % (float(value) * 100.0)


def _percent_or_dash(value: Any) -> str:
    return _percent(value) if value is not None else "—"


def _pp_or_dash(value: Any) -> str:
    return _percentage_points(value) if value is not None else "—"


def _delta_or_dash(value: Any) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number.is_integer():
        return "%+d" % int(number)
    return "%+.6g" % number


def _joined(values: Iterable[Any]) -> str:
    items = [str(item) for item in values]
    return ", ".join(items) if items else "—"


def _pair_text(left: str, right: str) -> str:
    return left if left == right else "%s → %s" % (left, right)


def _append_limitations(lines: List[str], limitations: Iterable[str]) -> None:
    values = tuple(limitations)
    if not values:
        return
    lines.extend(("", "## Limitations", ""))
    lines.extend("- %s" % _cell(item) for item in values)


def _append_simulated_banner(lines: List[str], simulated: bool) -> None:
    if simulated:
        lines.extend(
            (
                "> [!WARNING]",
                "> **SIMULATED RESULT** — FakeRuntime output validates the evaluation pipeline; "
                "it is not evidence of real model quality.",
                "",
            )
        )


__all__ = ["REPORT_SCHEMA_VERSION", "to_report_dict", "write_report"]
