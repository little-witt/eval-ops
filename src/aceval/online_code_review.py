"""Run reproducible code-review Skill evaluations through CATX sessions."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from .catx import CatxAgentClient, CatxAgentProfile
from .code_review import CodeReviewFindingsGrader, repository_observation
from .contracts import GradeResult, Oracle, as_primitive
from .execution_path import ExecutionPathSpec, evaluate_trace_conformance
from .task_center import TaskStore


ONLINE_CODE_REVIEW_API_VERSION = "aceval.online-code-review/v1"
DEFAULT_FRONTEND_STACKS = (
    "typescript-web",
    "react-native",
    "wechat-miniprogram",
)
EXPECTATIONS = (
    "Output is strict JSON with a findings array",
    "All Oracle findings are reported",
    "Every finding references an existing changed source line",
    "No unsupported finding is reported",
    "Tool results prove the exact Skill and Fixture commits were evaluated",
)


class OnlineCodeReviewError(ValueError):
    """Raised when an online code-review run cannot be prepared safely."""


@dataclass(frozen=True)
class OnlineReviewCase:
    id: str
    stack: str
    language: str
    case_type: str
    prompt: str
    base_ref: str
    head_ref: str
    base_commit: str
    head_commit: str
    oracle: Path


def _safe_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise OnlineCodeReviewError("%s must be a safe non-empty string" % label)
    return value


def _commit(value: Any, label: str) -> str:
    text = _safe_text(value, label)
    if len(text) != 40 or any(character not in "0123456789abcdef" for character in text):
        raise OnlineCodeReviewError("%s must be a full lowercase Git commit" % label)
    return text


def load_online_review_cases(
    lab_root: Union[str, Path],
    *,
    stacks: Sequence[str] = DEFAULT_FRONTEND_STACKS,
    case_ids: Sequence[str] = (),
) -> Tuple[OnlineReviewCase, ...]:
    root = Path(lab_root).expanduser().resolve()
    try:
        lab = json.loads((root / "lab.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OnlineCodeReviewError("cannot read Fixture Lab metadata") from exc
    raw_cases = lab.get("cases") if isinstance(lab, Mapping) else None
    if not isinstance(raw_cases, list):
        raise OnlineCodeReviewError("Fixture Lab cases must be an array")
    selected_stacks = set(stacks)
    selected_ids = set(case_ids)
    result = []
    for raw in raw_cases:
        if not isinstance(raw, Mapping):
            raise OnlineCodeReviewError("Fixture Lab case must be an object")
        case_id = _safe_text(raw.get("id"), "case.id")
        stack = _safe_text(raw.get("stack"), "case.stack")
        if selected_ids and case_id not in selected_ids:
            continue
        if not selected_ids and stack not in selected_stacks:
            continue
        case_path = root / "cases" / case_id / "case.json"
        try:
            case_document = json.loads(case_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OnlineCodeReviewError("cannot read case: %s" % case_id) from exc
        oracle = root / str(case_document.get("oracle", "oracle.json"))
        if not oracle.is_file():
            oracle = case_path.parent / str(case_document.get("oracle", "oracle.json"))
        if not oracle.is_file():
            raise OnlineCodeReviewError("case Oracle is missing: %s" % case_id)
        result.append(
            OnlineReviewCase(
                id=case_id,
                stack=stack,
                language=_safe_text(case_document.get("language"), "case.language"),
                case_type=_safe_text(case_document.get("case_type"), "case.case_type"),
                prompt=_safe_text(case_document.get("prompt"), "case.prompt"),
                base_ref=_safe_text(case_document.get("base_ref"), "case.base_ref"),
                head_ref=_safe_text(case_document.get("head_ref"), "case.head_ref"),
                base_commit=_commit(case_document.get("base_commit"), "case.base_commit"),
                head_commit=_commit(case_document.get("head_commit"), "case.head_commit"),
                oracle=oracle.resolve(),
            )
        )
    if selected_ids and {item.id for item in result} != selected_ids:
        missing = sorted(selected_ids - {item.id for item in result})
        raise OnlineCodeReviewError("unknown case ids: %s" % ", ".join(missing))
    if not result:
        raise OnlineCodeReviewError("online evaluation selected no cases")
    return tuple(result)


def build_online_review_prompt(
    case: OnlineReviewCase,
    *,
    skill_ref: str,
    skill_commit: str,
    skill_mount: str = "/workspace/skills/frontend-code-reviewer",
    fixture_mount: str = "/workspace/repo",
) -> str:
    ref = _safe_text(skill_ref, "skill_ref")
    commit = _commit(skill_commit, "skill_commit")
    return f"""你正在执行受控的代码评审 Skill 评测。不要创建提交或推送，不要修改 Skill 文件。

先准备并核对候选 Skill：
1. 在 {skill_mount} 执行 `git fetch origin {ref}`。
2. 执行 `git checkout --detach {commit}`，然后用 `git rev-parse HEAD` 确认必须精确等于 {commit}；不匹配就停止并报告错误。
3. 读取 {skill_mount}/src/SKILL.md，并按其中指引读取本次 {case.stack} 评审需要的 references 和 scripts。后续评审必须遵循这份候选 Skill。

再准备 Fixture PR 工作区：
1. 在 {fixture_mount} 执行 `git fetch origin {case.head_ref}`，确认 FETCH_HEAD 精确等于 {case.head_commit}。
2. 执行 `git checkout --detach {case.base_commit}`，确认 HEAD 精确等于 {case.base_commit}。
3. 执行 `git diff --binary {case.base_commit} {case.head_commit} | git apply`，把 PR 变更呈现为未暂存工作区 diff。
4. 用 `git diff --check`、`git diff --name-only` 和 `git diff` 核对待评审变更。不得改写或修复 Fixture 源码。

评测任务：
{case.prompt}

最终响应只能是严格 JSON，不能包含 Markdown 代码围栏、前后说明或额外字段。格式：
{{"findings":[{{"path":"relative/path","line":1,"category":"category","severity":"critical|high|medium|low","explanation":"concise actionable explanation"}}]}}
如果没有可操作缺陷，返回 {{"findings":[]}}。只报告本次 diff 新引入且能由代码证据支持的问题。"""


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _binding_verified(bundle: Any, case: OnlineReviewCase, skill_commit: str) -> Tuple[bool, str]:
    texts = []
    for event in bundle.trace:
        if event.kind != "tool_result":
            continue
        texts.append(json.dumps(as_primitive(event.payload), ensure_ascii=False, sort_keys=True))
    evidence = "\n".join(texts)
    missing = [
        label
        for label, value in (
            ("skill_commit", skill_commit),
            ("fixture_base_commit", case.base_commit),
            ("fixture_head_commit", case.head_commit),
        )
        if value not in evidence
    ]
    if missing:
        return False, "tool results do not prove: %s" % ", ".join(missing)
    return True, "tool results contain exact Skill, base, and head commits"


def _expectation_results(
    grade: GradeResult, binding_ok: bool, binding_evidence: str
) -> list[Mapping[str, Any]]:
    metrics = dict(grade.metrics)
    strict_json = "not valid strict JSON" not in grade.message and "must contain a findings array" not in grade.message
    values = (
        (EXPECTATIONS[0], strict_json, grade.message),
        (
            EXPECTATIONS[1],
            metrics.get("missed", math.inf) == 0,
            "missed=%s" % metrics.get("missed", "unavailable"),
        ),
        (
            EXPECTATIONS[2],
            metrics.get("invalid_source_references", math.inf) == 0,
            "invalid_source_references=%s"
            % metrics.get("invalid_source_references", "unavailable"),
        ),
        (
            EXPECTATIONS[3],
            metrics.get("false_positives", math.inf) == 0,
            "false_positives=%s" % metrics.get("false_positives", "unavailable"),
        ),
        (EXPECTATIONS[4], binding_ok, binding_evidence),
    )
    return [
        {"text": text, "passed": bool(passed), "evidence": evidence}
        for text, passed, evidence in values
    ]


def _session_payload(bundle: Any) -> Mapping[str, Any]:
    return {
        "schema_version": "aceval.imported-session/v1",
        "session_id": bundle.session_id,
        "profile_name": bundle.profile_name,
        "source": bundle.source,
        "completeness": bundle.completeness.as_dict(),
        "observation": as_primitive(bundle.observation),
    }


def run_online_review_case(
    client: CatxAgentClient,
    case: OnlineReviewCase,
    *,
    skill_ref: str,
    skill_commit: str,
    output_dir: Union[str, Path],
    title_prefix: str,
    poll_interval_seconds: float = 5.0,
    max_wait_seconds: float = 1200.0,
    infrastructure_retries: int = 1,
    execution_path: Optional[ExecutionPathSpec] = None,
) -> Mapping[str, Any]:
    run_dir = Path(output_dir)
    prompt = build_online_review_prompt(
        case, skill_ref=skill_ref, skill_commit=skill_commit
    )
    _write_json(run_dir / "request.json", {"title": f"{title_prefix} {case.id}", "prompt": prompt})
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    attempts = []
    bundle = None
    terminal = None
    for attempt in range(1, infrastructure_retries + 2):
        session_id = client.start_session(
            {"title": f"{title_prefix} {case.id} attempt-{attempt}", "prompt": prompt}
        )
        deadline = time.monotonic() + max_wait_seconds
        while time.monotonic() < deadline:
            terminal = client.poll_session(session_id)
            if terminal.get("status") in ("COMPLETED", "FAILED"):
                break
            time.sleep(poll_interval_seconds)
        else:
            raise TimeoutError("CATX session did not reach terminal state: %s" % session_id)
        bundle = client.fetch_session(session_id)
        attempts.append(
            {
                "attempt": attempt,
                "session_id": session_id,
                "terminal_status": terminal,
                "error": bundle.observation.error,
            }
        )
        error = str(bundle.observation.error or "")
        retryable = "频率限制" in error or "rate_limit" in error or "rate limited" in error.lower()
        if not retryable or attempt > infrastructure_retries:
            break
    if bundle is None:
        raise OnlineCodeReviewError("CATX did not return an imported run bundle")
    duration = time.monotonic() - started
    ended_at = datetime.now(timezone.utc)
    oracle_data = json.loads(case.oracle.read_text(encoding="utf-8"))
    observation = repository_observation(
        case.oracle.parents[2] / "repository",
        bundle.output,
        revision=case.head_commit,
    )
    grade = asyncio.run(
        CodeReviewFindingsGrader().evaluate(observation, Oracle(data=oracle_data), {})
    )
    binding_ok, binding_evidence = _binding_verified(bundle, case, skill_commit)
    expectations = _expectation_results(grade, binding_ok, binding_evidence)
    path_conformance = None
    if execution_path is not None:
        trace_complete = bool(bundle.completeness.trace) and not bool(
            bundle.observation.metadata.get("trace_may_be_truncated", False)
        )
        path_conformance = evaluate_trace_conformance(
            execution_path,
            [as_primitive(event) for event in bundle.trace],
            trace_complete=trace_complete,
        )
        expectations.append(
            {
                "text": "Execution path follows required Skill steps",
                "passed": path_conformance["status"] == "pass",
                "evidence": "trace_conformance=%s coverage=%s"
                % (path_conformance["status"], path_conformance["coverage"]),
                "status": path_conformance["status"],
            }
        )
    passed = sum(bool(item["passed"]) for item in expectations)
    tool_counts = {}
    errors = 0
    for event in bundle.trace:
        tool_counts[event.kind] = tool_counts.get(event.kind, 0) + 1
        if event.error:
            errors += 1
    total_tokens = int(bundle.usage.get("total_tokens", 0))
    grading = {
        "expectations": expectations,
        "summary": {
            "passed": passed,
            "failed": len(expectations) - passed,
            "total": len(expectations),
            "pass_rate": passed / len(expectations),
        },
        "execution_metrics": {
            "tool_calls": tool_counts,
            "total_tool_calls": sum(
                count for kind, count in tool_counts.items() if kind in ("tool_call", "tool_result")
            ),
            "total_steps": len(bundle.trace),
            "errors_encountered": errors + (1 if bundle.observation.error else 0),
            "output_chars": len(str(bundle.output or "")),
            "transcript_chars": len(json.dumps(_session_payload(bundle), ensure_ascii=False)),
        },
        "timing": {"total_duration_seconds": duration},
        "formal_grade": as_primitive(grade),
        "binding_verified": binding_ok,
    }
    outputs = run_dir / "outputs"
    _write_text(outputs / "final_output.txt", str(bundle.output or ""))
    _write_json(outputs / "session.json", _session_payload(bundle))
    _write_json(outputs / "attempts.json", attempts)
    if path_conformance is not None:
        _write_json(run_dir / "path_conformance.json", path_conformance)
    _write_json(run_dir / "grading.json", grading)
    _write_json(
        run_dir / "timing.json",
        {
            "total_tokens": total_tokens,
            "duration_ms": int(duration * 1000),
            "total_duration_seconds": duration,
            "executor_start": started_at.isoformat(),
            "executor_end": ended_at.isoformat(),
            "executor_duration_seconds": duration,
        },
    )
    _write_json(
        run_dir / "binding.json",
        {
            "api_version": ONLINE_CODE_REVIEW_API_VERSION,
            "session_id": bundle.session_id,
            "skill_ref": skill_ref,
            "skill_commit": skill_commit,
            "fixture_base_commit": case.base_commit,
            "fixture_head_commit": case.head_commit,
            "verified": binding_ok,
            "evidence": binding_evidence,
        },
    )
    return {
        "case_id": case.id,
        "session_id": bundle.session_id,
        "status": terminal.get("status") if terminal else None,
        "error": bundle.observation.error,
        "pass_rate": grading["summary"]["pass_rate"],
        "formal_status": str(grade.status.value),
        "binding_verified": binding_ok,
        "duration_seconds": duration,
        "total_tokens": total_tokens,
        "path_conformance": path_conformance,
    }


def run_online_review_suite(
    profile: CatxAgentProfile,
    *,
    lab_root: Union[str, Path],
    skill_ref: str,
    skill_commit: str,
    workspace: Union[str, Path],
    configuration: str,
    stacks: Sequence[str] = DEFAULT_FRONTEND_STACKS,
    case_ids: Sequence[str] = (),
    task_store: Optional[TaskStore] = None,
    task_id: Optional[str] = None,
    iteration: int = 0,
    execution_path: Optional[ExecutionPathSpec] = None,
) -> Mapping[str, Any]:
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    cases = load_online_review_cases(lab_root, stacks=stacks, case_ids=case_ids)
    client = CatxAgentClient(profile)
    results = []
    evals = []
    if task_id is not None and task_store is None:
        raise OnlineCodeReviewError("task_id requires a task_store")
    phase = "baseline" if configuration == "old_skill" else "candidate"
    if task_store and task_id:
        current = int(task_store.load(task_id).get("current_iteration", 0))
        task_store.update(task_id, status="running", current_iteration=max(current, iteration))
        task_store.append_event(
            task_id,
            "%s.started" % phase,
            {"configuration": configuration, "skill_commit": skill_commit, "case_count": len(cases)},
            iteration=iteration,
        )
    for index, case in enumerate(cases, 1):
        eval_dir = root / f"eval-{index:02d}-{case.id}"
        metadata = {
            "eval_id": index,
            "eval_name": case.id,
            "prompt": case.prompt,
            "assertions": list(EXPECTATIONS),
        }
        _write_json(eval_dir / "eval_metadata.json", metadata)
        run_dir = eval_dir / configuration / "run-1"
        if task_store and task_id:
            task_store.append_event(
                task_id,
                "case_run.started",
                {"configuration": configuration, "output_dir": str(run_dir)},
                iteration=iteration,
                case_id=case.id,
                run_id="run-1",
            )
        print("[%d/%d] starting %s" % (index, len(cases), case.id), flush=True)
        try:
            result = run_online_review_case(
                client,
                case,
                skill_ref=skill_ref,
                skill_commit=skill_commit,
                output_dir=run_dir,
                title_prefix=f"aceval {configuration}",
                execution_path=execution_path,
            )
        except Exception as exc:
            if task_store and task_id:
                task_store.append_event(
                    task_id,
                    "case_run.failed",
                    {"configuration": configuration, "error": str(exc), "output_dir": str(run_dir)},
                    iteration=iteration,
                    case_id=case.id,
                    run_id="run-1",
                )
                task_store.update(task_id, status="blocked")
            raise
        results.append(result)
        if task_store and task_id:
            task_store.append_event(
                task_id,
                "trace.captured",
                {
                    "session_id": result["session_id"],
                    "session_log": str(run_dir / "outputs" / "session.json"),
                    "attempt_log": str(run_dir / "outputs" / "attempts.json"),
                    "complete": result.get("path_conformance", {}).get("status") != "not_evaluable"
                    if result.get("path_conformance") else None,
                },
                iteration=iteration,
                case_id=case.id,
                run_id="run-1",
            )
            if result.get("path_conformance") is not None:
                task_store.append_event(
                    task_id,
                    "path_conformance.completed",
                    {
                        "result": result["path_conformance"],
                        "artifact": str(run_dir / "path_conformance.json"),
                    },
                    iteration=iteration,
                    case_id=case.id,
                    run_id="run-1",
                )
            task_store.append_event(
                task_id,
                "case_run.completed",
                {
                    "configuration": configuration,
                    "session_id": result["session_id"],
                    "status": result["status"],
                    "pass_rate": result["pass_rate"],
                    "grading": str(run_dir / "grading.json"),
                    "session_log": str(run_dir / "outputs" / "session.json"),
                },
                iteration=iteration,
                case_id=case.id,
                run_id="run-1",
            )
        evals.append(
            {
                "id": index,
                "prompt": case.prompt,
                "expected_output": (
                    "Strict JSON findings matching the frozen Oracle with no unsupported findings."
                ),
                "files": ["repository"],
                "expectations": list(EXPECTATIONS),
            }
        )
        print(
            "[%d/%d] finished %s pass_rate=%.2f status=%s"
            % (index, len(cases), case.id, result["pass_rate"], result["status"]),
            flush=True,
        )
    summary = {
        "api_version": ONLINE_CODE_REVIEW_API_VERSION,
        "configuration": configuration,
        "skill_ref": skill_ref,
        "skill_commit": skill_commit,
        "cases": results,
        "mean_pass_rate": sum(item["pass_rate"] for item in results) / len(results),
        "all_bindings_verified": all(item["binding_verified"] for item in results),
        "total_tokens": sum(item["total_tokens"] for item in results),
        "total_duration_seconds": sum(item["duration_seconds"] for item in results),
    }
    _write_json(root / f"suite-{configuration}.json", summary)
    _write_json(root.parent / "evals" / "evals.json", {"skill_name": "frontend-code-reviewer", "evals": evals})
    if task_store and task_id:
        task_store.append_event(
            task_id,
            "%s.completed" % phase,
            {
                "configuration": configuration,
                "mean_pass_rate": summary["mean_pass_rate"],
                "all_bindings_verified": summary["all_bindings_verified"],
                "suite_artifact": str(root / f"suite-{configuration}.json"),
            },
            iteration=iteration,
        )
        task_store.update(task_id, status="ready")
    return summary


def _benchmark_stats(values: Sequence[float]) -> Mapping[str, float]:
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}
    mean = sum(values) / len(values)
    variance = (
        sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        if len(values) > 1
        else 0.0
    )
    return {
        "mean": round(mean, 4),
        "stddev": round(math.sqrt(variance), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def build_online_review_benchmark(
    workspace: Union[str, Path],
    *,
    skill_name: str = "frontend-code-reviewer",
    skill_path: str = "",
    candidate: str = "with_skill",
    baseline: str = "old_skill",
) -> Mapping[str, Any]:
    """Aggregate exact CATX usage and grading data for a two-configuration workspace."""
    root = Path(workspace).expanduser().resolve()
    configurations = (candidate, baseline)
    grouped: dict[str, list[Mapping[str, Any]]] = {name: [] for name in configurations}
    runs = []
    eval_ids = []
    for eval_dir in sorted(root.glob("eval-*")):
        metadata = json.loads((eval_dir / "eval_metadata.json").read_text(encoding="utf-8"))
        eval_id = int(metadata["eval_id"])
        eval_name = str(metadata.get("eval_name") or eval_dir.name)
        eval_ids.append(eval_id)
        for configuration in configurations:
            run_dir = eval_dir / configuration / "run-1"
            grading_path = run_dir / "grading.json"
            timing_path = run_dir / "timing.json"
            if not grading_path.is_file() or not timing_path.is_file():
                raise OnlineCodeReviewError(
                    "benchmark input is incomplete: %s" % run_dir
                )
            grading = json.loads(grading_path.read_text(encoding="utf-8"))
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            summary = grading.get("summary", {})
            formal = grading.get("formal_grade", {})
            result = {
                "pass_rate": float(summary.get("pass_rate", 0.0)),
                "formal_pass": 1.0 if formal.get("status") == "pass" else 0.0,
                "passed": int(summary.get("passed", 0)),
                "failed": int(summary.get("failed", 0)),
                "total": int(summary.get("total", 0)),
                "time_seconds": float(timing.get("total_duration_seconds", 0.0)),
                "tokens": int(timing.get("total_tokens", 0)),
                "tool_calls": int(
                    grading.get("execution_metrics", {}).get("total_tool_calls", 0)
                ),
                "errors": int(
                    grading.get("execution_metrics", {}).get("errors_encountered", 0)
                ),
            }
            grouped[configuration].append(result)
            runs.append(
                {
                    "eval_id": eval_id,
                    "eval_name": eval_name,
                    "configuration": configuration,
                    "run_number": 1,
                    "result": result,
                    "expectations": grading.get("expectations", []),
                    "notes": [],
                }
            )

    run_summary: dict[str, Any] = {}
    for configuration in configurations:
        items = grouped[configuration]
        run_summary[configuration] = {
            "pass_rate": _benchmark_stats([item["pass_rate"] for item in items]),
            "formal_pass_rate": _benchmark_stats([item["formal_pass"] for item in items]),
            "time_seconds": _benchmark_stats([item["time_seconds"] for item in items]),
            "tokens": _benchmark_stats([float(item["tokens"]) for item in items]),
            "totals": {
                "time_seconds": round(sum(item["time_seconds"] for item in items), 4),
                "tokens": sum(item["tokens"] for item in items),
                "formal_passes": int(sum(item["formal_pass"] for item in items)),
                "cases": len(items),
            },
        }
    candidate_summary = run_summary[candidate]
    baseline_summary = run_summary[baseline]
    run_summary["delta"] = {
        "pass_rate": "%+.4f"
        % (
            candidate_summary["pass_rate"]["mean"]
            - baseline_summary["pass_rate"]["mean"]
        ),
        "formal_pass_rate": "%+.4f"
        % (
            candidate_summary["formal_pass_rate"]["mean"]
            - baseline_summary["formal_pass_rate"]["mean"]
        ),
        "time_seconds": "%+.1f"
        % (
            candidate_summary["time_seconds"]["mean"]
            - baseline_summary["time_seconds"]["mean"]
        ),
        "tokens": "%+.0f"
        % (
            candidate_summary["tokens"]["mean"]
            - baseline_summary["tokens"]["mean"]
        ),
    }
    return {
        "metadata": {
            "skill_name": skill_name,
            "skill_path": skill_path,
            "executor_model": "CATX configured model",
            "analyzer_model": "deterministic code_review_findings_v1",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "evals_run": sorted(set(eval_ids)),
            "runs_per_configuration": 1,
            "candidate": candidate,
            "baseline": baseline,
        },
        "runs": runs,
        "run_summary": run_summary,
        "notes": [
            "Each configuration was run once per case; cross-case standard deviation is not stochastic run variance.",
            "Strict JSON is a hard expectation; prose surrounding an otherwise correct JSON object remains a failure.",
            "Token counts come from CATX usage in timing.json, not output character counts.",
        ],
    }


def write_online_review_benchmark(
    workspace: Union[str, Path],
    *,
    skill_name: str = "frontend-code-reviewer",
    skill_path: str = "",
) -> Mapping[str, Any]:
    root = Path(workspace).expanduser().resolve()
    benchmark = build_online_review_benchmark(
        root, skill_name=skill_name, skill_path=skill_path
    )
    _write_json(root / "benchmark.json", benchmark)
    summary = benchmark["run_summary"]
    candidate = summary["with_skill"]
    baseline = summary["old_skill"]
    delta = summary["delta"]
    markdown = "\n".join(
        (
            "# Skill Benchmark: %s" % skill_name,
            "",
            "| Metric | Candidate | Baseline | Delta |",
            "|---|---:|---:|---:|",
            "| Assertion pass rate | %.1f%% | %.1f%% | %s |"
            % (
                candidate["pass_rate"]["mean"] * 100,
                baseline["pass_rate"]["mean"] * 100,
                delta["pass_rate"],
            ),
            "| Formal case pass rate | %.1f%% | %.1f%% | %s |"
            % (
                candidate["formal_pass_rate"]["mean"] * 100,
                baseline["formal_pass_rate"]["mean"] * 100,
                delta["formal_pass_rate"],
            ),
            "| Mean time / case | %.1fs | %.1fs | %ss |"
            % (
                candidate["time_seconds"]["mean"],
                baseline["time_seconds"]["mean"],
                delta["time_seconds"],
            ),
            "| Mean tokens / case | %.0f | %.0f | %s |"
            % (
                candidate["tokens"]["mean"],
                baseline["tokens"]["mean"],
                delta["tokens"],
            ),
            "",
            "Candidate totals: %d formal passes, %d tokens, %.1fs across %d cases."
            % (
                candidate["totals"]["formal_passes"],
                candidate["totals"]["tokens"],
                candidate["totals"]["time_seconds"],
                candidate["totals"]["cases"],
            ),
            "",
            "Baseline totals: %d formal passes, %d tokens, %.1fs across %d cases."
            % (
                baseline["totals"]["formal_passes"],
                baseline["totals"]["tokens"],
                baseline["totals"]["time_seconds"],
                baseline["totals"]["cases"],
            ),
            "",
            "Limitation: one run per case/configuration; no stochastic significance claim.",
            "",
        )
    )
    _write_text(root / "benchmark.md", markdown)
    return benchmark


__all__ = [
    "DEFAULT_FRONTEND_STACKS",
    "EXPECTATIONS",
    "ONLINE_CODE_REVIEW_API_VERSION",
    "OnlineCodeReviewError",
    "OnlineReviewCase",
    "build_online_review_prompt",
    "load_online_review_cases",
    "run_online_review_case",
    "run_online_review_suite",
    "build_online_review_benchmark",
    "write_online_review_benchmark",
]
