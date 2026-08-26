"""Compile optimization workspaces into a UI-safe convergence graph."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit, urlunsplit


OPTIMIZATION_GRAPH_API_VERSION = "aceval.optimization-graph/v1"
SECRET_KEY_PARTS = ("secret", "token", "password", "authorization", "api_key", "apikey")
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


def _run_payload(run_dir: Path) -> Mapping[str, Any]:
    request = run_dir / "request.json"
    grading_path = run_dir / "grading.json"
    timing_path = run_dir / "timing.json"
    binding_path = run_dir / "binding.json"
    if grading_path.is_file():
        status = "completed"
    elif request.is_file():
        status = "running"
    else:
        status = "pending"
    grading = _read_json(grading_path, required=False)
    timing = _read_json(timing_path, required=False)
    binding = _read_json(binding_path, required=False)
    summary = grading.get("summary") if isinstance(grading.get("summary"), Mapping) else {}
    formal = grading.get("formal_grade") if isinstance(grading.get("formal_grade"), Mapping) else {}
    return {
        "status": status,
        "pass_rate": float(summary.get("pass_rate", 0.0) or 0.0),
        "formal_pass": formal.get("status") == "pass",
        "tokens": int(timing.get("total_tokens", 0) or 0),
        "duration_seconds": float(timing.get("total_duration_seconds", 0.0) or 0.0),
        "binding_verified": bool(grading.get("binding_verified", binding.get("verified", False))),
        "expectations": grading.get("expectations", []) if isinstance(grading.get("expectations"), list) else [],
        "skill_commit": str(binding.get("skill_commit", "")),
        "session_id": str(binding.get("session_id", "")),
        "evidence": {
            "request": str(request),
            "grading": str(grading_path),
            "timing": str(timing_path),
            "binding": str(binding_path),
            "output": str(run_dir / "outputs" / "final_output.txt"),
            "session": str(run_dir / "outputs" / "session.json"),
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
    for eval_dir in sorted(iteration.glob("eval-*")):
        metadata = _read_json(eval_dir / "eval_metadata.json", required=False)
        case_id = str(metadata.get("eval_name") or eval_dir.name)
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
    return {
        "cases": len(values),
        "completed": len(completed),
        "pass_rate": (
            sum(float(item["pass_rate"]) for item in completed) / len(completed)
            if completed else None
        ),
        "formal_passes": sum(bool(item["formal_pass"]) for item in completed),
        "strict_json_passes": sum(
            bool(item["expectations"] and item["expectations"][0].get("passed"))
            for item in completed
        ),
        "tokens": sum(int(item["tokens"]) for item in completed),
        "duration_seconds": sum(float(item["duration_seconds"]) for item in completed),
        "bindings_verified": sum(bool(item["binding_verified"]) for item in completed),
        "skill_commit": next((item["skill_commit"] for item in completed if item["skill_commit"]), ""),
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
                if match is None:
                    continue
                measured += 1
                passed += bool(match.get("passed"))
                if not match.get("passed"):
                    evidence.append({"case_id": case["id"], "detail": str(match.get("evidence", ""))})
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
            "dimensions": [item["label"] for item in dimensions],
        },
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
