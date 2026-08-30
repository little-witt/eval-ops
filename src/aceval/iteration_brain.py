"""FORGE's local, staged analysis brain.

Evidence and the final control-plane decision are deterministic.  The model
is used only for three small, schema checked jobs: semantic grading,
attribution, and proposal writing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from .agent_runtime import ModelClient, ReferenceRuntimeError
from .claude_profiles import ClaudeProfileError
from .evidence_analysis import _evidence_issue, _heuristic_case_result, _optimization_case_ready, compact_case_evidence
from .kernel_contracts import ANALYSIS_DECISION_API_VERSION
from .kernel_v2 import build_case_aggregates, compile_diagnosis_graph

EVIDENCE_BUNDLE_API_VERSION = "aceval.evidence-bundle/v1"
SEMANTIC_VERDICT_API_VERSION = "aceval.semantic-verdict/v1"
ATTRIBUTION_REPORT_API_VERSION = "aceval.attribution-report/v1"
OPTIMIZATION_PLAN_API_VERSION = "aceval.optimization-plan/v1"
AGENT_CALL_RECEIPT_API_VERSION = "aceval.agent-call-receipt/v1"
PROMPT_TEMPLATE_VERSION = "local-forge-staged-analysis/v2"


# These labels are part of the decision read model, not a renderer-only
# translation.  Exporting a human-readable label with every roll-up keeps
# archived decisions understandable when they are opened outside Electron.
_DIMENSION_LABELS_CN = {
    "outcome": "结果是否符合 Case 目标",
    "grounding": "结论是否有证据支撑",
    "evidence_grounding": "证据引用是否可追溯",
    "runtime": "运行过程中是否报错",
    "efficiency": "Token 与运行成本",
    "procedure": "是否按 Skill 要求执行",
    "safety_side_effect": "是否产生不安全副作用",
    "format": "输出格式是否符合要求",
    "binding": "仓库版本是否正确绑定",
    "path": "执行路径是否符合约束",
    "reliability": "重复运行是否稳定",
    "evidence": "会话证据是否完整",
}


def _dimension_base(value: Any) -> str:
    return str(value or "unknown").split(":", 1)[0]


def _dimension_label_cn(value: Any) -> str:
    raw = str(value or "未命名维度")
    base = _dimension_base(raw)
    label = _DIMENSION_LABELS_CN.get(base, base.replace("_", " "))
    return label if ":" not in raw else "%s（%s）" % (label, raw.split(":", 1)[1])


def _dimension_summaries(attempts: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Collapse repeated attempt dimensions into one readable Case view.

    The raw per-attempt verdict remains available in ``attempts``.  Reviewers
    should not have to scan five copies of the same 100% card to understand a
    Case, so the decision exposes a conservative roll-up first.
    """

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for attempt in attempts:
        for dimension in attempt.get("dimensions", ()) if isinstance(attempt, Mapping) else ():
            if not isinstance(dimension, Mapping):
                continue
            key = str(dimension.get("dimension") or "unknown")
            grouped.setdefault(key, []).append(dimension)
    summaries: list[Mapping[str, Any]] = []
    for key, values in grouped.items():
        statuses = [str(item.get("status") or "not_evaluable") for item in values]
        if "fail" in statuses:
            status = "fail"
        elif "not_evaluable" in statuses:
            status = "not_evaluable"
        elif statuses and all(item == "pass" for item in statuses):
            status = "pass"
        else:
            status = statuses[0] if statuses else "not_evaluable"
        scores = [
            float(item["score"])
            for item in values
            if isinstance(item.get("score"), (int, float)) and not isinstance(item.get("score"), bool)
        ]
        reasons = list(dict.fromkeys(str(item.get("reason") or "") for item in values if str(item.get("reason") or "")))
        refs = list(dict.fromkeys(str(ref) for item in values for ref in item.get("evidence_refs", ()) if str(ref)))
        summaries.append({
            "dimension": key,
            "label": _dimension_label_cn(key),
            "status": status,
            "score": sum(scores) / len(scores) if scores else None,
            "hard": any(item.get("hard") is True for item in values),
            "attempt_count": len(values),
            "reason": "；".join(reasons[:3]) or "没有记录该维度的证据说明",
            "evidence_refs": refs[:8],
            "source": str(values[0].get("source") or "fact"),
        })
    return summaries


def _dimension_evidence_detail(
    dimension: Mapping[str, Any],
    evidence: Mapping[str, Any],
    case: Mapping[str, Any],
) -> str:
    """Explain what in the immutable output/Trace supports a dimension score.

    The score and status are still produced by the deterministic Kernel.  This
    helper only builds a reviewer-facing bridge from that grade to concrete
    evidence, so a user can understand a failed dimension without opening a
    raw JSON artifact first.
    """

    name = str(dimension.get("dimension") or "unknown")
    base = _dimension_base(name)
    status = str(dimension.get("status") or "not_evaluable")
    trace = evidence.get("trace") if isinstance(evidence.get("trace"), Mapping) else {}
    output = str(evidence.get("output_excerpt") or "").replace("\r", " ").replace("\n", " ").strip()
    output = output if len(output) <= 360 else output[:360] + "…"
    refs = [str(ref) for ref in dimension.get("evidence_refs", ()) if str(ref)]
    artifact = str(evidence.get("artifact") or "")
    selected = trace.get("selected_evidence") if isinstance(trace.get("selected_evidence"), Sequence) else ()
    selected = [item for item in selected if isinstance(item, Mapping)]
    selected_indexes = [str(item.get("index")) for item in selected[:4] if item.get("index") is not None]

    if base in {"outcome", "format"}:
        expected = case.get("expected_output")
        if expected is None:
            # An open-ended Case intentionally has no exact string oracle.
            # Calling that absence an ``expected`` value made the UI look
            # self-contradictory ("expected: not declared").
            if status == "fail":
                return "未声明精确期望；依据 Case 目标与实际输出「%s」判定未满足。" % (output or "（空）")
            if status == "pass":
                return "未声明精确期望；仅记录实际输出「%s」，需结合目标与语义标准理解。" % (output or "（空）")
            return "未声明精确期望；实际输出「%s」不足以形成确定判定。" % (output or "（空）")
        expected_text = expected if isinstance(expected, str) else json.dumps(expected, ensure_ascii=False, sort_keys=True, default=str)
        expected_text = expected_text if len(expected_text) <= 220 else expected_text[:220] + "…"
        if status == "fail":
            return "依据输出通道：期望「%s」；实际「%s」。" % (expected_text, output or "（空）")
        return "依据输出通道：实际输出为「%s」。%s" % (output or "（空）", "与期望一致。" if status == "pass" else "")
    if base in {"procedure", "path"}:
        path = evidence.get("path_conformance") if isinstance(evidence.get("path_conformance"), Mapping) else {}
        steps = path.get("steps") if isinstance(path.get("steps"), Sequence) else ()
        missing = [
            str(item.get("label") or item.get("id"))
            for item in steps
            if isinstance(item, Mapping)
            and (item.get("observed") is not True)
            and item.get("kind") in {"required", "alternative"}
        ]
        forbidden = [
            str(item.get("label") or item.get("id"))
            for item in steps
            if isinstance(item, Mapping)
            and item.get("kind") == "forbidden"
            and item.get("observed") is True
        ]
        if missing or forbidden:
            detail = []
            if missing:
                detail.append("未观察到必须步骤：%s" % "、".join(missing[:5]))
            if forbidden:
                detail.append("观察到禁止步骤：%s" % "、".join(forbidden[:5]))
            return "依据执行路径 Trace：%s。" % "；".join(detail)
        return "依据执行路径 Trace：观察到 %d 个路径检查点；%s" % (len(steps), str(dimension.get("reason") or "路径符合要求"))
    if base == "runtime":
        errors = trace.get("errors") if isinstance(trace.get("errors"), Sequence) else ()
        error_text = [str(item.get("error")) for item in errors if isinstance(item, Mapping) and item.get("error")]
        return "依据 Trace[%s]：%s。" % (
            ", ".join(selected_indexes) or "完整事件序列",
            "；".join(error_text[:4]) if error_text else "未观察到工具/运行时错误",
        )
    if base in {"grounding", "evidence_grounding", "evidence"}:
        if refs:
            return "依据不可变证据引用：%s。" % "、".join(refs[:6])
        return "已形成输出/Trace，但没有指向本次会话的独立证据引用。"
    if base == "efficiency":
        usage = evidence.get("usage") if isinstance(evidence.get("usage"), Mapping) else {}
        return "依据运行收据：%s。" % (
            str(dimension.get("reason") or "未记录 Token 使用量")
            + ("（总 Token=%s）" % usage.get("total_tokens") if usage.get("total_tokens") is not None else "")
        )
    if artifact and selected_indexes:
        return "依据不可变产物 %s#trace[%s]；%s" % (artifact, ",".join(selected_indexes), str(dimension.get("reason") or "已记录相关证据"))
    return str(dimension.get("reason") or "没有绑定可核验的输出或 Trace 内容")


def _enrich_attempt_dimensions(
    attempts: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    case: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    enriched = []
    for attempt in attempts:
        value = dict(attempt)
        dimensions = []
        for raw in attempt.get("dimensions", ()) if isinstance(attempt, Mapping) else ():
            if not isinstance(raw, Mapping):
                continue
            dimension = dict(raw)
            dimension["evidence_detail"] = _dimension_evidence_detail(dimension, evidence, case)
            dimensions.append(dimension)
        value["dimensions"] = dimensions
        enriched.append(value)
    return enriched


def _text_list(value: Any) -> list[str]:
    """Normalize model factor fields so the renderer never hides a string."""

    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _assessment_failure_facts(assessment: Mapping[str, Any]) -> list[str]:
    """Collect concrete failed-dimension facts for summaries and proposals."""

    facts = []
    for dimension in assessment.get("dimension_summaries", ()) if isinstance(assessment.get("dimension_summaries"), Sequence) else ():
        if not isinstance(dimension, Mapping) or dimension.get("status") not in ("fail", "not_evaluable"):
            continue
        label = str(dimension.get("label") or _dimension_label_cn(dimension.get("dimension")))
        score = dimension.get("score")
        score_text = "" if score is None else "（评分 %.0f%%）" % (float(score) * 100)
        detail = str(dimension.get("evidence_detail") or dimension.get("reason") or "未记录具体证据")
        facts.append("%s%s：%s" % (label, score_text, detail))
    missing = (assessment.get("goal_observations", {}) or {}).get("missing_requirements", ())
    if isinstance(missing, Sequence) and not isinstance(missing, (str, bytes)) and missing:
        facts.append("目标未观察到：%s" % "、".join(str(item) for item in missing[:5]))
    return list(dict.fromkeys(facts))


def _short_text(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _concise_failure_summary(assessments: Sequence[Mapping[str, Any]]) -> str:
    """Summarize repeated Case failures by dimension, not by raw Case prose."""

    groups: dict[str, dict[str, Any]] = {}
    for assessment in assessments:
        for dimension in assessment.get("dimension_summaries", ()) if isinstance(assessment.get("dimension_summaries"), Sequence) else ():
            if not isinstance(dimension, Mapping) or dimension.get("status") not in ("fail", "not_evaluable"):
                continue
            key = str(dimension.get("dimension") or "unknown")
            group = groups.setdefault(key, {"label": str(dimension.get("label") or _dimension_label_cn(key)), "cases": set(), "detail": ""})
            group["cases"].add(str(assessment.get("case_id") or ""))
            if not group["detail"]:
                group["detail"] = _short_text(dimension.get("evidence_detail") or dimension.get("reason") or "未记录具体依据")
    lines = []
    for group in groups.values():
        count = len({case_id for case_id in group["cases"] if case_id})
        lines.append("%s：%d 个 Case；%s" % (group["label"], count, group["detail"]))
    return "；".join(lines[:4])


def _concrete_skill_change(assessments: Sequence[Mapping[str, Any]], case_ids: Sequence[str]) -> str:
    """Create a useful deterministic fallback when the model omits a proposal."""

    selected = [assessment for assessment in assessments if str(assessment.get("case_id")) in set(str(item) for item in case_ids)]
    facts = _concise_failure_summary(selected)
    if not facts:
        facts = "补充与本轮失败事实对应的执行要求和完成前自检"
    return (
        "修改 SKILL.md：在对应执行流程中补齐「%s」；"
        "在最终输出前逐项自检并报告核对结果，确保后续 Case 能在 Trace/输出中观察到这些产物。"
        % facts
    )


def _hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class EvidenceQueryError(ValueError):
    pass


@dataclass
class LocalEvidenceQueryPort:
    """Bounded, read-only access; it has no shell, git, or write operation."""
    task_root: Path
    max_bytes: int = 256_000
    max_queries: int = 32
    _queries: int = 0

    def _artifact(self, reference: str) -> Path:
        root = self.task_root.expanduser().resolve()
        path = Path(reference).expanduser().resolve()
        if root not in path.parents or not path.is_file() or path.is_symlink():
            raise EvidenceQueryError("artifact is outside the task evidence directory")
        return path

    def read_log_window(self, artifact: str, start: int = 0, limit: int = 16_000) -> Mapping[str, Any]:
        if self._queries >= self.max_queries:
            raise EvidenceQueryError("evidence query limit exceeded")
        # Count attempted queries as well as successful reads.  Otherwise a
        # caller could probe an unbounded number of paths after the quota was
        # reached.
        self._queries += 1
        if start < 0 or limit <= 0 or limit > self.max_bytes:
            raise EvidenceQueryError("invalid bounded log window")
        path = self._artifact(artifact)
        with path.open("rb") as stream:
            stream.seek(start)
            data = stream.read(limit)
        return {"artifact": str(path), "start": start, "bytes": len(data), "text": data.decode("utf-8", errors="replace")}

    def get_case_summary(self, case_id: str, evidence: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        for item in evidence:
            if str(item.get("case_id")) == case_id:
                return dict(item)
        raise EvidenceQueryError("case is not present in the frozen evidence bundle")


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("%s must be a JSON object" % label)
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("%s is not strict JSON" % label) from exc


def _model_compact(value: Any, *, max_string: int = 1800) -> Any:
    """Bound model-only context without changing the archived evidence."""
    if isinstance(value, Mapping):
        return {str(key): _model_compact(item, max_string=max_string) for key, item in value.items()}
    if isinstance(value, list):
        return [_model_compact(item, max_string=max_string) for item in value]
    if isinstance(value, tuple):
        return [_model_compact(item, max_string=max_string) for item in value]
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string] + "…[truncated for model context]"
    return value


def _model_case_context(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Keep high-signal case evidence while bounding repeated trace detail.

    The complete run artifact remains immutable on disk.  This projection is
    only what is sent to Claude, so a large trace or EvalPack envelope cannot
    crowd out the case identity, outcome, integrity status, and artifact ref.
    """
    result = _model_compact(value, max_string=1200)
    if not isinstance(result, Mapping):
        return value
    result = dict(result)
    trace = result.get("trace")
    if isinstance(trace, Mapping):
        trace = dict(trace)
        selected = trace.get("selected_evidence")
        if isinstance(selected, list):
            trace["selected_evidence"] = [
                dict(item) if isinstance(item, Mapping) else item
                for item in selected[:8]
            ]
            for item in trace["selected_evidence"]:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    item["text"] = item["text"][:700]
        if isinstance(trace.get("errors"), list):
            trace["errors"] = trace["errors"][:8]
        result["trace"] = trace
    formal = result.get("formal_grading")
    if isinstance(formal, Mapping):
        formal = dict(formal)
        if isinstance(formal.get("grades"), list):
            formal["grades"] = formal["grades"][:12]
        result["formal_grading"] = formal
    return result


def _case_review_context(case: Mapping[str, Any], evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build the per-Case contract sent to the semantic reviewer.

    The context intentionally keeps the Case objective, declared checks,
    frozen path and transport timing beside the trace summary.  This prevents
    a model from grading a reply in isolation and makes retry/latency and
    branch-binding failures visible as separate attribution candidates.
    """

    metadata = case.get("metadata") if isinstance(case.get("metadata"), Mapping) else {}
    test = metadata.get("aceval_test") if isinstance(metadata.get("aceval_test"), Mapping) else {}
    dimensions = test.get(
        "scoring_dimensions",
        test.get("dimensions", metadata.get("scoring_dimensions", metadata.get("dimensions", ()))),
    )
    if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)):
        dimensions = []
    focus = test.get(
        "focus_checks",
        test.get("重点检查项", metadata.get("focus_checks", metadata.get("重点检查项", ()))),
    )
    if not isinstance(focus, Sequence) or isinstance(focus, (str, bytes)):
        focus = []
    return {
        "case_id": str(case.get("id") or ""),
        "goal": str(case.get("prompt") or ""),
        "expected_output": case.get("expected_output"),
        "expected_records": case.get("expected_records"),
        "forbidden_records": case.get("forbidden_records"),
        "expected_observables": test.get("expected_observables", metadata.get("expected_observables", [])),
        "重点检查项": [str(item) for item in focus],
        "scoring_dimensions": [str(item) for item in dimensions] or [
            "outcome", "procedure", "grounding", "evidence_grounding",
            "path", "binding", "runtime", "safety_side_effect",
            "format", "efficiency", "reliability",
        ],
        "execution_path": metadata.get("execution_path") or test.get("execution_path") or None,
        "source_refs": list(test.get("source_refs", ())) if isinstance(test.get("source_refs", ()), Sequence) and not isinstance(test.get("source_refs", ()), (str, bytes)) else [],
        "artifact": evidence.get("artifact"),
        "trace": evidence.get("trace"),
        "trace_complete": evidence.get("trace_complete") is True,
        "timing": evidence.get("timing", {}),
        "retry_count": evidence.get("retry_count", 0),
        "attempts": evidence.get("attempts", []),
        "binding_status": evidence.get("binding_status"),
        "path_conformance": evidence.get("path_conformance"),
        "output_excerpt": evidence.get("output_excerpt", ""),
    }


_GOAL_STOPWORDS = frozenset({
    "the", "and", "with", "from", "that", "this", "must", "should",
    "have", "has", "are", "was", "for", "into", "then", "最终", "包含",
    "输出", "结果", "报告", "需要", "应该", "能够", "完成",
})


def _goal_tokens(value: Any) -> list[str]:
    """Extract distinctive words used only for deterministic evidence lookup."""

    text = str(value or "").casefold()
    tokens = re.findall(r"[a-z0-9][a-z0-9_./:+-]{1,}|[\u4e00-\u9fff]{2,}", text)
    return [token for token in tokens if token not in _GOAL_STOPWORDS and len(token) >= 2]


def _case_goal_observations(
    case: Mapping[str, Any],
    evidence: Mapping[str, Any],
    result: Mapping[str, Any],
    aggregate: Any,
    issue: Optional[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Explain each declared Case observable against the immutable trace.

    This is deliberately a conservative index, not a second grader.  It never
    changes the Case verdict; it tells the reviewer which declared goal text
    was found in output/Trace and which goal could not be observed.
    """

    metadata = case.get("metadata") if isinstance(case.get("metadata"), Mapping) else {}
    test = metadata.get("aceval_test") if isinstance(metadata.get("aceval_test"), Mapping) else {}
    declared = test.get("expected_observables")
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
        declared = []
    goals = [str(item).strip() for item in declared if str(item).strip()]
    if not goals and case.get("expected_output") is not None:
        goals = ["输出满足 Case 的期望结果"]

    trace = evidence.get("trace") if isinstance(evidence.get("trace"), Mapping) else {}
    selected = trace.get("selected_evidence") if isinstance(trace.get("selected_evidence"), Sequence) else []
    selected = [item for item in selected if isinstance(item, Mapping)]
    output = str(evidence.get("output_excerpt") or "")
    corpus_parts = [output]
    corpus_parts.extend(str(item.get("text") or "") for item in selected)
    corpus_parts.extend(str(key) for key in (trace.get("tools") or {}) if key)
    corpus_parts.extend(str(item.get("error") or "") for item in (trace.get("errors") or ()) if isinstance(item, Mapping))
    path = evidence.get("path_conformance") if isinstance(evidence.get("path_conformance"), Mapping) else None
    if path:
        corpus_parts.append(str(path.get("reason") or ""))
        corpus_parts.extend(str(item.get("label") or "") for item in (path.get("steps") or ()) if isinstance(item, Mapping))
    corpus = "\n".join(corpus_parts).casefold()
    artifact = str(evidence.get("artifact") or "")
    trace_complete = evidence.get("trace_complete") is True
    trace_event_count = int(trace.get("event_count") or 0) if str(trace.get("event_count") or "").isdigit() else 0
    trace_analyzed = bool(artifact and trace_complete and trace_event_count > 0)
    evidence_evaluable = bool(artifact) and not (aggregate is not None and aggregate.status == "not_evaluable")
    items: list[Mapping[str, Any]] = []

    def refs_for(needle: str, tokens: Sequence[str]) -> list[str]:
        refs: list[str] = []
        lowered = needle.casefold()
        if lowered and lowered in output.casefold() and artifact:
            refs.append(artifact + "#output")
        for item in selected:
            text = str(item.get("text") or "").casefold()
            if lowered and (lowered in text or any(token in text for token in tokens)):
                if artifact:
                    refs.append("%s#trace[%s]" % (artifact, item.get("index", "?")))
                break
        return list(dict.fromkeys(refs))

    for goal in goals:
        tokens = _goal_tokens(goal)
        matched_tokens = [token for token in tokens if token in corpus]
        exact = bool(goal and goal.casefold() in corpus)
        distinctive_match = bool(matched_tokens) and (
            exact
            or len(matched_tokens) >= max(1, (len(tokens) + 1) // 2)
            or any(token.endswith(".js") or token in {"git", "diff", "trace", "pr"} for token in matched_tokens)
        )
        refs = refs_for(goal, tokens)
        if not evidence_evaluable:
            status = "not_evaluable"
            explanation = "当前不能核对该目标：" + str((issue or {}).get("reason_cn") or "会话证据或判定依据未就绪")
            refs = [artifact] if artifact else []
        elif distinctive_match:
            status = "observed"
            explanation = "已在本次会话输出或 Trace 中观察到与该目标对应的证据。"
        else:
            status = "not_observed"
            explanation = "完整日志中未观察到与该目标对应的明确工具调用、产物或输出内容；不能把它当作已完成。"
        items.append({
            "requirement": goal,
            "status": status,
            "explanation": explanation,
            "evidence_refs": refs,
            "matched_tokens": matched_tokens[:8],
        })

    # Execution-path rows are first-class observations as well.  Keeping them
    # alongside expected_observables makes it clear whether a missing goal is
    # a Skill output problem or a missing/violated procedure step.
    if path and isinstance(path.get("steps"), Sequence):
        for step in path.get("steps", ()):
            if not isinstance(step, Mapping) or step.get("kind") not in {"required", "forbidden"}:
                continue
            label = str(step.get("label") or step.get("id") or "执行步骤")
            observed = step.get("observed") is True
            indexes = step.get("evidence_event_indexes") if isinstance(step.get("evidence_event_indexes"), Sequence) else []
            refs = ["%s#trace[%s]" % (artifact, index) for index in indexes[:4] if artifact]
            if not evidence_evaluable:
                status = "not_evaluable"
                explanation = "当前不能核对该执行步骤：" + str((issue or {}).get("reason_cn") or "Trace 未证明完整")
            elif step.get("kind") == "forbidden" and observed:
                status = "violated"
                explanation = "实际 Trace 观察到禁止步骤；这会影响执行路径符合度。"
            elif step.get("kind") == "required" and not observed:
                status = "not_observed"
                explanation = "完整 Trace 中未观察到该必须步骤；请检查 Skill 是否执行了声明流程。"
            else:
                status = "observed"
                explanation = "实际 Trace 观察到该执行步骤。"
            items.append({
                "requirement": "执行路径：" + label,
                "status": status,
                "explanation": explanation,
                "evidence_refs": refs,
                "source": "execution_path",
            })

    observed_count = sum(item.get("status") == "observed" for item in items)
    missing = [item.get("requirement") for item in items if item.get("status") in {"not_observed", "violated"}]
    return {
        "title": str(test.get("title") or case.get("title") or case.get("id") or "未命名 Case"),
        "prompt": str(case.get("prompt") or ""),
        "summary": str(test.get("generation_reason") or test.get("selection_reason") or "验证 Skill 声明的能力与风险"),
        "items": items,
        "observed_count": observed_count,
        "total_count": len(items),
        "missing_requirements": missing,
        "trace_analyzed": trace_analyzed,
        "trace_complete": trace_complete,
        "trace_event_count": trace_event_count,
        "output_chars": int(evidence.get("output_chars") or len(output)),
        "artifact": artifact or None,
    }


def _overall_assessment(
    cases: Sequence[Mapping[str, Any]],
    assessments: Sequence[Mapping[str, Any]],
    clusters: Sequence[Mapping[str, Any]],
    changes: Sequence[Mapping[str, Any]],
    evidence_issues: Sequence[Mapping[str, Any]],
    next_action: str,
) -> Mapping[str, Any]:
    """Build the Chinese, cross-Case optimization hand-off shown in the UI."""

    stable = [item for item in assessments if item.get("attribution") == "stable_pass"]
    candidates = [item for item in assessments if item.get("attribution") == "skill_improvement_candidate"]
    pending = [item for item in assessments if item.get("attribution") == "pass_pending_verification"]
    gaps = [item for item in assessments if item.get("attribution") == "evidence_gap"]
    skill_problems = []
    for cluster in clusters:
        # This column is specifically the Skill problem summary.  Evaluator,
        # Agent/model and environment findings belong in the attribution
        # matrix and must not be presented as Skill defects.
        if cluster.get("skill_change_authorized") is not True:
            continue
        ids = [str(item) for item in cluster.get("case_ids", ())]
        if not ids:
            continue
        facts = [item for item in assessments if item.get("case_id") in ids]
        refs = [ref for item in facts for dim in item.get("dimensions", ()) for ref in dim.get("evidence_refs", ())]
        missing = [
            requirement
            for item in facts
            for requirement in (item.get("goal_observations", {}) or {}).get("missing_requirements", ())
            if requirement
        ]
        raw_problem = str(cluster.get("root_cause_hypothesis") or cluster.get("root_cause") or cluster.get("problem") or cluster.get("hypothesis") or "")
        dimension_facts = [fact for item in facts for fact in _assessment_failure_facts(item)]
        dimension_facts = list(dict.fromkeys(dimension_facts))
        affected = list(dict.fromkeys(
            str(item.get("label") or _dimension_label_cn(item.get("dimension")))
            for assessment in facts
            for item in assessment.get("dimension_summaries", ())
            if isinstance(item, Mapping) and item.get("status") in ("fail", "not_evaluable")
        ))
        problem = "Skill 缺口：%s（影响 %d 个 Case）" % (
            "、".join(affected[:4]) or "Case 要求未被稳定落实",
            len(ids),
        )
        fact_reasons = list(dict.fromkeys(str(item.get("reason") or "") for item in cluster.get("facts", ()) if isinstance(item, Mapping) and item.get("reason")))
        if fact_reasons:
            dimension_facts.extend(fact_reasons)
        concise_facts = _concise_failure_summary(facts) or _short_text("；".join(dimension_facts), 420)
        if missing:
            missing_unique = list(dict.fromkeys(str(item) for item in missing))
            concise_facts = "%s；未观察到：%s" % (concise_facts, "、".join(missing_unique[:3]))
        skill_problems.append({
            "case_ids": ids,
            "problem": problem,
            "problem_cn": problem,
            "fact_summary": _short_text(concise_facts, 520),
            "hypothesis": _short_text(raw_problem or "待通过下一轮回归验证的共性原因", 240),
            "affected_dimensions": affected,
            "evidence_refs": list(dict.fromkeys(refs))[:8],
            "authorized": True,
            "classification": "skill",
        })
    # Do not let the two summary panels disagree when the attribution model
    # returned proposals/candidates without a populated cluster.  Every
    # authorized candidate must have a corresponding visible Skill problem.
    represented = {case_id for item in skill_problems for case_id in item.get("case_ids", ())}
    for assessment in candidates:
        case_id = str(assessment.get("case_id") or "")
        if not case_id or case_id in represented:
            continue
        refs = [ref for dimension in assessment.get("dimensions", ()) for ref in dimension.get("evidence_refs", ())]
        skill_problems.append({
            "case_ids": [case_id],
            "problem": "Skill 缺口：%s（影响 1 个 Case）" % ("、".join(str(item.get("label") or _dimension_label_cn(item.get("dimension"))) for item in assessment.get("dimension_summaries", ()) if isinstance(item, Mapping) and item.get("status") in ("fail", "not_evaluable")) or "Case 目标未满足"),
            "problem_cn": "Skill 缺口：%s（影响 1 个 Case）" % ("、".join(str(item.get("label") or _dimension_label_cn(item.get("dimension"))) for item in assessment.get("dimension_summaries", ()) if isinstance(item, Mapping) and item.get("status") in ("fail", "not_evaluable")) or "Case 目标未满足"),
            "fact_summary": _short_text(_concise_failure_summary([assessment]) or str(assessment.get("reason") or "未记录具体事实"), 520),
            "hypothesis": _short_text(str(assessment.get("attribution_hypothesis") or "待通过下一轮回归验证的共性原因"), 240),
            "affected_dimensions": [str(item.get("label") or _dimension_label_cn(item.get("dimension"))) for item in assessment.get("dimension_summaries", ()) if isinstance(item, Mapping) and item.get("status") in ("fail", "not_evaluable")],
            "evidence_refs": list(dict.fromkeys(refs))[:8],
            "authorized": True,
            "classification": "skill",
        })
    if skill_problems:
        # Present one comprehensive Skill diagnosis, not one card per branch
        # or per failure cluster.  The detailed Case evidence remains
        # available in the per-Case assessment and attribution matrix.
        skill_problems = [{
            "case_ids": list(dict.fromkeys(case_id for item in skill_problems for case_id in item.get("case_ids", ()))),
            "problem": "；".join(dict.fromkeys(str(item.get("problem") or "") for item in skill_problems if item.get("problem"))),
            "problem_cn": "；".join(dict.fromkeys(str(item.get("problem_cn") or item.get("problem") or "") for item in skill_problems if item.get("problem_cn") or item.get("problem"))),
            "fact_summary": _short_text("；".join(dict.fromkeys(str(item.get("fact_summary") or "") for item in skill_problems if item.get("fact_summary"))), 760),
            "hypothesis": _short_text("；".join(dict.fromkeys(str(item.get("hypothesis") or "") for item in skill_problems if item.get("hypothesis"))), 420),
            "affected_dimensions": list(dict.fromkeys(str(value) for item in skill_problems for value in item.get("affected_dimensions", ()) if str(value))),
            "evidence_refs": list(dict.fromkeys(ref for item in skill_problems for ref in item.get("evidence_refs", ())))[:12],
            "authorized": True,
            "classification": "skill",
        }]
    optimization_plan = []
    for change in changes:
        change_text = str(change.get("change") or "根据失败证据修正 Skill")
        related_ids = [str(item) for item in change.get("case_ids", ()) if str(item)]
        generic_markers = (
            "根据失败维度和目标核对结果修正 Skill",
            "根据失败证据修正 Skill",
            "repair the shared cause supported by failed case evidence",
            "根据失败 Case 的证据修复共性问题",
            "补充明确的执行步骤、产出要求和自检标准",
        )
        if not change_text.strip() or any(marker.casefold() in change_text.casefold() for marker in generic_markers):
            change_text = _concrete_skill_change(assessments, related_ids)
        related = [item for item in clusters if set(str(case_id) for case_id in item.get("case_ids", ())).intersection(related_ids)]
        root_causes = [str(item.get("root_cause_hypothesis") or item.get("root_cause") or item.get("problem") or item.get("hypothesis") or "") for item in related]
        root_causes = [item for item in root_causes if item and item not in {"unresolved", "requires cross-case Skill repair analysis"}]
        why = str(change.get("why") or "")
        if not why or why in {"失败 Case 的可核验证据", "one or more frozen expectations failed"}:
            related_assessments = [item for item in assessments if str(item.get("case_id")) in set(related_ids)]
            facts = _concise_failure_summary(related_assessments)
            why = facts or ("根因假设：" + _short_text("；".join(dict.fromkeys(root_causes)), 240) if root_causes else "对应失败 Case 的维度证据已绑定到不可变会话产物")
        optimization_plan.append({
            "target": str(change.get("target") or "SKILL.md"),
            "change": change_text,
            "why": why,
            "case_ids": related_ids,
            "target_guidance": str(change.get("target_guidance") or change.get("section") or "在 Skill 对应能力段落补充失败事实所要求的执行与自检规则"),
            "validation_steps": _text_list(change.get("validation_steps") or change.get("verification_steps")) or ["使用同一批失败 Case 复验，并确认所有保护 Case 仍通过"],
        })
    if candidates or optimization_plan:
        conclusion = "本轮发现 %d 条可归因到 Skill 的改进候选；%d 条稳定通过 Case 应继续作为回归保护。" % (len(candidates), len(stable))
        next_step = "先结合用户意见和下方证据支持的建议优化 Skill，再使用同一批冻结 Case 与测试分支进入下一轮验证。"
        action = "optimize_skill_then_verify"
    elif gaps:
        conclusion = "本轮有 %d 条 Case 证据不足；不能据此断言 Skill 有问题。%d 条 Case 已稳定通过。" % (len(gaps), len(stable))
        next_step = "先补齐会话日志、Case/Attempt 绑定或可信通过标准；证据可核验后，再决定是否需要优化 Skill 并进入下一轮验证。"
        action = "resolve_evidence_before_optimization"
    elif pending:
        conclusion = "本轮已有 %d 条 Case 初测通过，但仍有 %d 条等待稳定性复验；当前没有已证实的 Skill 缺陷。" % (len(pending), len(pending))
        next_step = "先完成同一测试分支上的稳定性复验；复验通过后保持 Skill，不进入修改流程。"
        action = "verify_passes"
    else:
        conclusion = "本轮没有形成可授权的 Skill 修改；请以逐 Case 目标核对和证据状态为准。"
        next_step = "先确认缺失证据或通过标准，再决定是否进入 Skill 优化和下一轮验证。"
        action = next_action
    fixture_follow_up = [
        {
            "case_id": str(item.get("case_id")),
            "evidence_type": str(item.get("evidence_type") or "unknown"),
            "reason": str(item.get("reason_cn") or item.get("reason") or "证据缺口"),
            "action": str(item.get("guidance") or "定向重试该 Case"),
        }
        for item in evidence_issues
    ]
    # Goal coverage is explanatory rather than a replacement for the hard
    # verdict.  This is the bridge between a user's Case objective and the
    # deterministic aggregate shown above it.
    goal_items = [
        goal_item
        for assessment in assessments
        for goal_item in (assessment.get("goal_observations", {}) or {}).get("items", ())
        if isinstance(goal_item, Mapping)
    ]
    observed_goals = sum(item.get("status") == "observed" for item in goal_items)
    unobserved_goals = sum(item.get("status") in {"not_observed", "violated"} for item in goal_items)
    unevaluable_goals = sum(item.get("status") == "not_evaluable" for item in goal_items)
    dimension_totals: dict[str, dict[str, Any]] = {}
    for assessment in assessments:
        for dimension in assessment.get("dimension_summaries", ()):
            if not isinstance(dimension, Mapping):
                continue
            key = str(dimension.get("dimension") or "unknown")
            row = dimension_totals.setdefault(key, {
                "dimension": key,
                "label": str(dimension.get("label") or _dimension_label_cn(key)),
                "case_count": 0,
                "pass_count": 0,
                "fail_count": 0,
                "not_evaluable_count": 0,
                "hard": False,
            })
            row["case_count"] += 1
            row["pass_count"] += dimension.get("status") == "pass"
            row["fail_count"] += dimension.get("status") == "fail"
            row["not_evaluable_count"] += dimension.get("status") == "not_evaluable"
            row["hard"] = row["hard"] or dimension.get("hard") is True
    if candidates or optimization_plan:
        skill_decision = {
            "status": "needs_improvement",
            "title": "建议先优化 Skill，再进入下一轮验证",
            "summary": "至少一个可信失败 Case 与 Skill 能力缺陷相关；测试分支证据可以支持修改，但修改后必须用同一批 Case 回归。",
        }
    elif gaps:
        skill_decision = {
            "status": "evidence_incomplete",
            "title": "暂不修改 Skill，先补齐证据",
            "summary": "当前有 Case 无法核对完整会话或通过标准；证据不足不等于 Skill 失败。",
        }
    elif pending:
        skill_decision = {
            "status": "verification_pending",
            "title": "暂不修改 Skill，先完成稳定性复验",
            "summary": "目前只有初测通过，尚未达到规定的重复运行次数。",
        }
    else:
        skill_decision = {
            "status": "no_change",
            "title": "当前没有需要修改 Skill 的可信证据",
            "summary": "现有 Case 均已通过确定性门禁，或没有形成可授权的失败归因。",
        }
    case_outcome_counts = {
        "stable_pass": len(stable),
        "not_passed": len(candidates) + len([item for item in assessments if item.get("attribution") == "failure_not_authorized"]),
        "evidence_gap": len(gaps),
        "verification_pending": len(pending),
    }
    all_case_ids = [str(item.get("id") or "") for item in cases if item.get("id")]
    stable_case_ids = [str(item.get("case_id")) for item in stable]
    pending_case_ids = [str(item.get("case_id")) for item in pending]
    failed_case_ids = [
        str(item.get("case_id"))
        for item in assessments
        if item.get("status") == "fail" and item.get("case_id")
    ]
    cluster_by_case: dict[str, Mapping[str, Any]] = {}
    for cluster in clusters:
        if not isinstance(cluster, Mapping):
            continue
        for case_id in cluster.get("case_ids", ()) if isinstance(cluster.get("case_ids"), Sequence) else ():
            cluster_by_case.setdefault(str(case_id), cluster)
    responsibility_labels = {
        "skill_improvement_candidate": "Skill 问题候选",
        "failure_not_authorized": "评测/证据不足，暂不归因 Skill",
        "evidence_gap": "证据或环境问题",
        "pass_pending_verification": "待稳定性复验",
        "stable_pass": "当前无问题",
    }
    causal_matrix = [
        (lambda cluster, responsibility, facts: {
            "case_id": str(item.get("case_id")),
            "verdict": str(item.get("status") or "unknown"),
            "deterministic_responsibility": responsibility,
            "responsibility_label": responsibility_labels.get(responsibility, responsibility.replace("_", " ")),
            "hypothesis": _short_text((cluster or {}).get("root_cause_hypothesis") if isinstance(cluster, Mapping) else None, 240),
            "fact_summary": _short_text(_concise_failure_summary([item]) or ("该 Case 的硬维度与证据门均通过；继续作为回归保护" if responsibility == "stable_pass" else str(item.get("reason") or "当前没有可核验事实")), 420),
            "failure_dimensions": [
                {
                    "dimension": str(dimension.get("dimension") or "unknown"),
                    "label": str(dimension.get("label") or _dimension_label_cn(dimension.get("dimension"))),
                    "status": str(dimension.get("status") or "not_evaluable"),
                    "score": dimension.get("score"),
                    "reason": _short_text(dimension.get("reason") or dimension.get("evidence_detail") or "", 180),
                    "evidence_detail": _short_text(dimension.get("evidence_detail") or "", 220),
                    "evidence_refs": list(dimension.get("evidence_refs", ())),
                }
                for dimension in item.get("dimension_summaries", ())
                if isinstance(dimension, Mapping) and dimension.get("status") in ("fail", "not_evaluable")
            ],
            "skill_factors": _text_list(item.get("skill_factors")) or (_text_list((cluster or {}).get("skill_factors")) if isinstance(cluster, Mapping) else []),
            "agent_model_factors": _text_list(item.get("agent_model_factors")) or (_text_list((cluster or {}).get("agent_model_factors")) if isinstance(cluster, Mapping) else []),
            "environment_factors": _text_list(item.get("environment_factors")) or (_text_list((cluster or {}).get("environment_factors")) if isinstance(cluster, Mapping) else []),
            "attribution_confidence": item.get("attribution_confidence") or ((cluster or {}).get("confidence") if isinstance(cluster, Mapping) else None),
            "evidence_refs": [
                ref
                for dimension in item.get("dimensions", ())
                if isinstance(dimension, Mapping)
                for ref in dimension.get("evidence_refs", ())
            ][:8],
        })(
            cluster_by_case.get(str(item.get("case_id"))),
            str(item.get("attribution") or "unknown"),
            _assessment_failure_facts(item),
        )
        for item in assessments
    ]
    return {
        "conclusion": conclusion,
        "conclusion_cn": conclusion,
        "skill_decision": skill_decision,
        "case_outcome_counts": case_outcome_counts,
        "goal_coverage": {
            "observed": observed_goals,
            "not_observed": unobserved_goals,
            "not_evaluable": unevaluable_goals,
            "total": len(goal_items),
        },
        "dimension_summary": list(dimension_totals.values()),
        "skill_problems": skill_problems,
        "skill_optimization_plan": optimization_plan,
        "fixture_follow_up": fixture_follow_up,
        "causal_matrix": causal_matrix,
        "optimization_summary": {
            "skill_problem_count": len(candidates),
            "skill_problem_case_ids": [str(item.get("case_id")) for item in candidates],
            "protected_case_ids": sorted(set(stable_case_ids + pending_case_ids)),
            "conflict_count": len([item for item in clusters if item.get("conflict")]) + len([item for item in changes if item.get("conflict")]),
            "proposed_change_count": len(optimization_plan),
            "must_validate_case_ids": sorted(set(failed_case_ids)),
        },
        "validation_plan": {
            "strategy": "same_frozen_case_and_fixture_regression",
            "reuse_evaluation_flow": True,
            "reuse_case_revisions": True,
            "reuse_fixture_branches": True,
            "required_case_ids": sorted(set(failed_case_ids)),
            "protected_case_ids": sorted(set(stable_case_ids + pending_case_ids)),
            "retire_case_after_stable_rounds": 2,
            "next_round_action": "optimize_skill_then_verify" if (candidates or optimization_plan) else action,
        },
        "analysis_method": {
            "per_case_independent": True,
            "full_trace_artifact_bound": True,
            "cross_case_summary_after_case_reviews": True,
            "remote_session_recreated": False,
        },
        "next_step": next_step,
        "next_action": action,
        "counts": {
            "case_count": len(cases),
            "stable_pass": len(stable),
            "skill_candidates": len(candidates),
            "pending_verification": len(pending),
            "evidence_gaps": len(gaps),
        },
    }


def _json(content: str, label: str) -> Mapping[str, Any]:
    if not isinstance(content, str):
        raise ValueError("%s returned invalid JSON" % label)
    text = content.lstrip("\ufeff").strip()
    candidates = [text]
    # Claude occasionally wraps an otherwise valid answer in Markdown even
    # when asked for JSON-only output.  Accept only a fenced JSON body, never
    # arbitrary Markdown as a substitute for the contract.
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE):
        candidates.append(match.group(1).strip())
    # Also tolerate a short natural-language preface/suffix around one JSON
    # object.  raw_decode guarantees that the selected fragment itself is
    # valid JSON; schema validation still happens in the stage parser.
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            candidates.append(text[index:index + end])
            break
    try:
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, list) and label == "semantic_grading":
                    # Some Claude aliases follow the requested case-results
                    # shape but omit the outer envelope. Normalize that safe
                    # shorthand before the schema parser validates coverage.
                    parsed = {"case_results": parsed}
                return _object(parsed, label)
            except json.JSONDecodeError:
                continue
        raise json.JSONDecodeError("no JSON object found", text, 0)
    except json.JSONDecodeError as exc:
        raise ValueError("%s returned invalid JSON" % label) from exc


def _case_rows(value: Any, ids: Sequence[str], label: str, *, verification: bool = True) -> list[Mapping[str, Any]]:
    # Claude sometimes emits a compact object keyed by case id instead of an
    # array.  Normalize that additive representation before applying the
    # strict coverage/status checks below.
    if isinstance(value, Mapping):
        rows = []
        for case_id, item in value.items():
            if isinstance(item, Mapping):
                row = dict(item)
            elif isinstance(item, str):
                # Compact model responses occasionally use {case_id:
                # "pass"}.  Keep the raw token and let the normalization
                # below handle localized/qualified variants as well.
                row = {"status": item, "reason": "model returned compact status", "evidence_refs": []}
            else:
                raise ValueError("%s.case_results must contain objects" % label)
            row.setdefault("case_id", str(case_id))
            rows.append(row)
    elif isinstance(value, list) and all(isinstance(x, Mapping) for x in value):
        rows = [dict(x) for x in value]
    else:
        raise ValueError("%s.case_results must be an array or case-id object" % label)
    if {str(x.get("case_id")) for x in rows} != set(ids):
        raise ValueError("%s.case_results do not cover the selected cases" % label)
    for row in rows:
        # Normalize common model wording to the frozen contract before
        # validation.  Real local-model responses often use Chinese labels,
        # uppercase values, ``verdict``/``case_status`` aliases, or a short
        # qualifier such as ``fail: skill``.  These are unambiguous and safe
        # to normalize; arbitrary prose is downgraded to ``not_evaluable``
        # below so it cannot authorize a mutation.
        raw_status = row.get("status")
        if raw_status is None:
            for alias in ("verdict", "case_status", "result_status", "outcome"):
                if row.get(alias) is not None:
                    raw_status = row.get(alias)
                    break
        if isinstance(raw_status, Mapping):
            raw_status = raw_status.get("status") or raw_status.get("label") or raw_status.get("value")
        status_text = str(raw_status).strip().casefold() if raw_status is not None else ""
        status_key = re.sub(r"[\s\-]+", "_", status_text)
        status_aliases = {
            "pass": "pass", "passed": "pass", "passing": "pass", "success": "pass", "successful": "pass", "ok": "pass", "positive": "pass",
            "通过": "pass", "已通过": "pass", "通过了": "pass", "成功": "pass", "符合": "pass", "满足": "pass",
            "fail": "fail", "failed": "fail", "failure": "fail", "not_passed": "fail", "not-pass": "fail", "negative": "fail",
            "未通过": "fail", "不通过": "fail", "失败": "fail", "不符合": "fail", "不满足": "fail", "拒绝": "fail",
            "not_evaluable": "not_evaluable", "not_evaluated": "not_evaluable", "not_evaluable_case": "not_evaluable", "pending": "not_evaluable", "unknown": "not_evaluable", "unclear": "not_evaluable", "inconclusive": "not_evaluable", "partial": "not_evaluable", "unscored": "not_evaluable", "not_scored": "not_evaluable", "unable_to_evaluate": "not_evaluable", "not_applicable": "not_evaluable", "insufficient_evidence": "not_evaluable", "error": "not_evaluable",
            "无法评估": "not_evaluable", "不可评估": "not_evaluable", "无法评价": "not_evaluable", "未评估": "not_evaluable", "证据不足": "not_evaluable", "证据不充分": "not_evaluable", "无法判断": "not_evaluable", "无法确定": "not_evaluable", "待定": "not_evaluable", "不适用": "not_evaluable",
        }
        normalized_status = status_aliases.get(status_key) or status_aliases.get(status_text)
        if normalized_status is None:
            # Qualifiers are accepted only when the leading token is one of
            # the known labels, e.g. ``fail (missing output)``.
            for prefix, normalized in (("pass", "pass"), ("fail", "fail"), ("not_evaluable", "not_evaluable"), ("not evaluable", "not_evaluable"), ("通过", "pass"), ("未通过", "fail"), ("证据不足", "not_evaluable"), ("无法评估", "not_evaluable"), ("不可评估", "not_evaluable"), ("无法判断", "not_evaluable")):
                if status_text.startswith(prefix) and (len(status_text) == len(prefix) or status_text[len(prefix)] in " \t(（):：,，。；;"):
                    normalized_status = normalized
                    break
        if normalized_status is not None:
            row["status"] = normalized_status
        if row.get("status") not in ("pass", "fail", "not_evaluable"):
            # Do not strand the whole confirmation flow on one free-form
            # model token.  An unknown semantic status is conservatively
            # downgraded to ``not_evaluable`` (and retained for audit) so it
            # cannot authorize a Skill edit; the UI can still show the model
            # warning alongside the evidence gap.
            row["status_raw"] = raw_status
            row["status"] = "not_evaluable"
            row["reason"] = (str(row.get("reason") or "") + "；模型返回未识别状态：%s" % (raw_status or "（空）")).strip("；")
        if verification:
            raw_verification = row.get("verification_status", "not_run")
            verification_text = str(raw_verification).strip().casefold()
            verification_key = re.sub(r"[\s\-]+", "_", verification_text)
            verification_alias = status_aliases.get(verification_key) or status_aliases.get(verification_text)
            if verification_alias is not None:
                row["verification_status"] = verification_alias
            elif verification_text in {"not_run", "not started", "未运行", "未开始"}:
                row["verification_status"] = "not_run"
            elif row.get("verification_status") not in ("pass", "fail", "not_evaluable", "not_run"):
                row["verification_status_raw"] = raw_verification
                row["verification_status"] = "not_evaluable"
        if not isinstance(row.get("reason", ""), str) or not isinstance(row.get("evidence_refs", []), list):
            raise ValueError("%s case row has invalid reason/evidence_refs" % label)
    return rows


def _parse_semantic(value: Mapping[str, Any], ids: Sequence[str], baseline_ids: Sequence[str]) -> Mapping[str, Any]:
    # Accept additive model fields (v3 dimensions, confidence, rationale,
    # etc.) while freezing only the contract fields below.  The extension
    # payload is retained for explainability but never drives deterministic
    # pass/fail or mutation authorization decisions.
    # Accept a wrapped envelope and common model aliases.  Only the normalized
    # case rows are used for the frozen decision; wrapper/extension metadata is
    # retained for explainability.
    payload: Mapping[str, Any] = value
    for wrapper in ("semantic_verdict", "result", "analysis"):
        nested = payload.get(wrapper) if isinstance(payload, Mapping) else None
        if isinstance(nested, Mapping) and ("case_results" in nested or "results" in nested):
            payload = nested
            break
    if "case_results" not in payload:
        for alias in ("results", "case_scores", "scores", "cases"):
            if alias in payload:
                payload = dict(payload)
                payload["case_results"] = payload[alias]
                break
    known = {"api_version", "case_results", "without_skill_baseline_case_results", "failure_clusters", "conflicts", "proposed_changes", "target_scope", "model_extensions", "semantic_verdict", "result", "analysis", "results", "case_scores", "scores", "cases"}
    extensions = {str(key): value[key] for key in value if key not in known}
    if isinstance(value.get("model_extensions"), Mapping):
        extensions.update(dict(value["model_extensions"]))
    rows = _case_rows(payload.get("case_results"), ids, "semantic verdict")
    baseline = []
    if baseline_ids:
        baseline_value = payload.get("without_skill_baseline_case_results")
        if baseline_value is None:
            baseline_value = payload.get("baseline_results")
        baseline = _case_rows(baseline_value, baseline_ids, "semantic baseline", verification=False)
    elif payload.get("without_skill_baseline_case_results"):
        raise ValueError("semantic baseline was returned without requested cases")
    result = {"case_results": rows, "without_skill_baseline_case_results": baseline}
    if extensions:
        result["model_extensions"] = _model_compact(extensions, max_string=1200)
    return result


def _parse_attribution(value: Mapping[str, Any]) -> Mapping[str, Any]:
    known = {"api_version", "failure_clusters", "conflicts", "case_results", "proposed_changes", "target_scope", "without_skill_baseline_case_results", "model_extensions"}
    for key in ("failure_clusters", "conflicts"):
        if not isinstance(value.get(key), list) or any(not isinstance(x, Mapping) for x in value[key]):
            raise ValueError("attribution %s must be an array of objects" % key)
    result = {"failure_clusters": [dict(x) for x in value["failure_clusters"]], "conflicts": [dict(x) for x in value["conflicts"]]}
    extensions = {str(key): value[key] for key in value if key not in known}
    if isinstance(value.get("model_extensions"), Mapping):
        extensions.update(dict(value["model_extensions"]))
    if extensions:
        result["model_extensions"] = _model_compact(extensions, max_string=1200)
    return result


def _parse_proposal(value: Mapping[str, Any], inventory: Sequence[str]) -> Mapping[str, Any]:
    known = {"api_version", "proposed_changes", "target_scope", "case_results", "failure_clusters", "conflicts", "without_skill_baseline_case_results", "model_extensions"}
    changes, scope = value.get("proposed_changes"), value.get("target_scope")
    if not isinstance(changes, list) or any(not isinstance(x, Mapping) for x in changes):
        raise ValueError("optimization proposed_changes must be an array of objects")
    if not isinstance(scope, list) or any(not isinstance(x, str) or not x for x in scope):
        raise ValueError("optimization target_scope must be an array of paths")
    allowed = set(str(x) for x in inventory)
    if any(x not in allowed for x in scope):
        raise ValueError("optimization target_scope contains a non-editable path")
    result = {"proposed_changes": [dict(x) for x in changes], "target_scope": list(scope)}
    extensions = {str(key): value[key] for key in value if key not in known}
    if isinstance(value.get("model_extensions"), Mapping):
        extensions.update(dict(value["model_extensions"]))
    if extensions:
        result["model_extensions"] = _model_compact(extensions, max_string=1200)
    return result


class IterationBrain:
    def __init__(self, model: Optional[ModelClient], artifact_root: Path, *, provider: str = "local-forge", profile: str = "default", model_id: Optional[str] = None, max_retries: int = 1, max_evidence_chars_per_case: int = 4000, max_prompt_chars: int = 48_000, required_k: int = 2, stage_callback: Optional[Callable[[str], None]] = None) -> None:
        if isinstance(required_k, bool) or not isinstance(required_k, int) or not 1 <= required_k <= 20:
            raise ValueError("required_k must be between 1 and 20")
        self.model, self.artifact_root = model, Path(artifact_root)
        self.provider = provider
        model_profile = getattr(model, "profile", None)
        if callable(model_profile):
            model_profile = model_profile()
        self.profile = dict(model_profile) if isinstance(model_profile, Mapping) else str(model_profile or profile)
        self.model_id = str(getattr(model, "model_id", None) or model_id or "unconfigured")
        self.max_retries = max(0, max_retries)
        self.max_evidence_chars_per_case, self.max_prompt_chars = max_evidence_chars_per_case, max_prompt_chars
        self.required_k = required_k
        self.receipts: list[Mapping[str, Any]] = []
        self.query_receipts: list[Mapping[str, Any]] = []
        self.current_receipts: list[Mapping[str, Any]] = []
        self.current_query_receipts: list[Mapping[str, Any]] = []
        self.stage_callback = stage_callback
        self._load_receipt_history()
        self._prompt_chars = 0
        self._attempt_counter = self._next_attempt()

    def _load_receipt_history(self) -> None:
        """Load immutable root receipts so a restarted Brain never reuses names."""
        def load_many(pattern: str) -> list[Mapping[str, Any]]:
            values = []
            for path in sorted(self.artifact_root.glob(pattern)):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, Mapping): values.append(value)
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
            return values
        if self.artifact_root.is_dir():
            self.receipts.extend(load_many("agent-call-receipt-*.json"))
            self.query_receipts.extend(load_many("evidence-query-receipt-*.json"))

    def _next_attempt(self) -> int:
        values = []
        if self.artifact_root.is_dir():
            for p in self.artifact_root.glob("attempt-*"):
                try: values.append(int(p.name.split("-", 1)[1]))
                except (IndexError, ValueError): pass
        return max(values, default=0)

    def _record_receipt(self, receipt: Mapping[str, Any], stage_dir: Optional[Path] = None) -> None:
        value = dict(receipt); self.receipts.append(value); self.current_receipts.append(value)
        index = max([int(p.stem.rsplit("-", 1)[1]) for p in self.artifact_root.glob("agent-call-receipt-*.json") if p.stem.rsplit("-", 1)[-1].isdigit()] or [0]) + 1
        _write(self.artifact_root / ("agent-call-receipt-%03d.json" % index), value)
        if stage_dir: _write(stage_dir / "agent-call-receipt.json", value)

    def _record_query(self, stage: str, case_id: str, artifact: Optional[str], result: Optional[Mapping[str, Any]], error: Optional[str]) -> None:
        value = {"api_version": "aceval.evidence-query-receipt/v1", "stage": stage, "case_id": case_id, "artifact": artifact, "status": "failed" if error else "succeeded", "query": {"start": 0, "limit": 4096}}
        if result: value.update({"output_hash": _hash(result), "bytes": result.get("bytes", 0)})
        if error: value["error"] = error[:500]
        self.query_receipts.append(value); self.current_query_receipts.append(value)
        index = max([int(p.stem.rsplit("-", 1)[1]) for p in self.artifact_root.glob("evidence-query-receipt-*.json") if p.stem.rsplit("-", 1)[-1].isdigit()] or [0]) + 1
        _write(self.artifact_root / ("evidence-query-receipt-%03d.json" % index), value)

    def _load_stage(self, stage: str, input_hash: str, parser: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
        for manifest_path in sorted(self.artifact_root.glob("attempt-*/%s/manifest.json" % stage)):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("status") != "validated" or manifest.get("input_hash") != input_hash: continue
                artifact_path = manifest_path.parent / "artifact.json"
                value = _object(json.loads(artifact_path.read_text(encoding="utf-8")), stage)
                parsed = parser(value)
                if manifest.get("output_hash") == _hash(parsed): return parsed
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return None

    def _save_stage(self, stage: str, input_hash: str, value: Mapping[str, Any], attempt: int) -> Mapping[str, Any]:
        parsed = _object(value, stage); stage_dir = self.artifact_root / ("attempt-%03d" % attempt) / stage
        _write(stage_dir / "artifact.json", parsed)
        _write(stage_dir / "manifest.json", {"api_version": "aceval.stage-manifest/v1", "stage": stage, "attempt": attempt, "input_hash": input_hash, "output_hash": _hash(parsed), "status": "validated", "artifact_validated": True, "artifact": str(stage_dir / "artifact.json")})
        aliases = {"evidence_compilation": "evidence-bundle.json", "semantic_grading": "semantic-verdict.json", "attribution": "attribution-report.json", "proposal": "optimization-plan.json"}
        if stage in aliases: _write(self.artifact_root / aliases[stage], parsed)
        return parsed

    def _failed_stage(self, stage: str, input_hash: str, attempt: int, exc: Exception) -> None:
        stage_dir = self.artifact_root / ("attempt-%03d" % attempt) / stage
        _write(stage_dir / "manifest.json", {"api_version": "aceval.stage-manifest/v1", "stage": stage, "attempt": attempt, "input_hash": input_hash, "status": "failed", "artifact_validated": False, "error": str(exc)[:500]})

    def _model_stage(self, stage: str, input_value: Mapping[str, Any], parser: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> Mapping[str, Any]:
        input_hash = _hash(input_value); reused = self._load_stage(stage, input_hash, parser)
        if reused is not None: return reused
        if self.model is None: raise ValueError("local-forge model is not configured")
        if self.stage_callback is not None:
            self.stage_callback(stage)
        self._attempt_counter += 1; attempt = self._attempt_counter; stage_dir = self.artifact_root / ("attempt-%03d" % attempt) / stage
        request_value = input_value
        request_text = json.dumps(request_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        # Keep the stage input identity immutable, but progressively compact
        # only the payload sent to the local model.  This protects retries
        # from repeatedly failing on the same oversized evidence envelope.
        for limit in (1200, 800, 500, 320, 220, 140, 90, 60):
            if len(request_text) <= self.max_prompt_chars:
                break
            request_value = _model_compact(input_value, max_string=limit)
            request_text = json.dumps(request_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(request_text) > self.max_prompt_chars and isinstance(request_value, Mapping):
            request_value = dict(request_value)
            request_value.pop("windows", None)
            # Keep the complete immutable artifact references, timing and
            # verdict fields, but omit repeated trace excerpts/attempt blobs
            # from the transport envelope as a last-resort budget safeguard.
            # The local evidence query port can still fetch bounded windows by
            # artifact when a reviewer needs deeper detail.
            for key in ("verification_evidence", "without_skill_baseline_evidence"):
                if isinstance(request_value.get(key), list):
                    compact_rows = []
                    for row in request_value[key]:
                        if not isinstance(row, Mapping):
                            continue
                        value = dict(row)
                        trace = value.get("trace")
                        if isinstance(trace, Mapping):
                            trace = dict(trace)
                            trace["selected_evidence"] = []
                            value["trace"] = trace
                        value.pop("attempts", None)
                        compact_rows.append(value)
                    request_value[key] = compact_rows
            if isinstance(request_value.get("evidence"), list):
                compact_rows = []
                for row in request_value["evidence"]:
                    if not isinstance(row, Mapping):
                        continue
                    value = dict(row)
                    trace = value.get("trace")
                    if isinstance(trace, Mapping):
                        trace = dict(trace)
                        trace["selected_evidence"] = []
                        value["trace"] = trace
                    value.pop("attempts", None)
                    compact_rows.append(value)
                request_value["evidence"] = compact_rows
            request_text = json.dumps(request_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(request_text) > self.max_prompt_chars and isinstance(request_value, Mapping):
            # Final deterministic fallback for very small test budgets: keep
            # identifiers, statuses and immutable artifact refs so the model
            # can still return a schema-valid per-Case verdict.
            request_value = dict(request_value)
            if isinstance(request_value.get("cases"), list):
                request_value["cases"] = [
                    {"id": str(item.get("id")), "prompt": str(item.get("prompt") or "")[:120]}
                    for item in request_value["cases"] if isinstance(item, Mapping)
                ]
            request_value.pop("case_review_context", None)
            request_value.pop("review_contract", None)
            request_text = json.dumps(request_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        started = time.monotonic()
        receipt = {"api_version": AGENT_CALL_RECEIPT_API_VERSION, "provider": self.provider, "profile": self.profile, "model": self.model_id, "prompt_template_version": PROMPT_TEMPLATE_VERSION, "stage": stage, "attempt": attempt, "input_hash": _hash(request_text), "request_chars": len(request_text), "request_estimated_tokens": max(1, (len(request_text) + 3) // 4), "context_compacted": request_text != json.dumps(input_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False), "status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
        try:
            if len(request_text) > self.max_prompt_chars:
                raise ValueError("staged request exceeds Token budget after safe evidence compaction (stage=%s, chars=%d, budget=%d)" % (stage, len(request_text), self.max_prompt_chars))
            self._prompt_chars += len(request_text)
            contract_hint = {
                "semantic_grading": "必须包含 case_results（数组；每项含 case_id、status、reason、evidence_refs；建议同时返回 dimensions、skill_factors、agent_model_factors、environment_factors、attribution、attribution_confidence）。每次只分析一个 Case，并结合 Skill 原文、goal、重点检查项、scoring_dimensions、execution_path、完整 Trace、输出、耗时和重试记录；每个失败维度必须说明对应的输出或 Trace 内容，明确 Skill/Agent/环境归因。只有请求了基线时才返回 without_skill_baseline_case_results。",
                "attribution": "必须包含 failure_clusters（数组）和 conflicts（数组）；结合逐 Case 结论与 Skill 原文区分 Skill 本身问题、Agent/模型执行偏差、工具/环境问题和评测标准问题，并给出可证伪的根因假设、对应事实、影响 Case、Skill 原文依据和置信度；不得只罗列 Case 名称或日志路径。",
                "proposal": "必须包含 proposed_changes（数组）和 target_scope（可编辑路径数组）；每个 proposed_change 必须写明 Skill 原文中的目标段落/规则、具体新增或改写内容、解决的证据事实、受影响与保护 Case、验证步骤；禁止返回泛化的‘根据失败维度修改 Skill’。",
            }.get(stage, "严格遵循当前阶段契约字段。")
            reply = self.model.complete([{"role": "system", "content": "请为 %s 阶段只返回一个 JSON 对象。%s 所有 reason、root_cause、change、why、description 和 evidence 说明必须使用简体中文。不要使用 Markdown 围栏、前后解释、注释或多个对象；reason 保持在 240 字以内。" % (stage, contract_hint)}, {"role": "user", "content": request_text}], ())
            receipt.update({"usage": dict(reply.usage or {}), "duration_ms": int((time.monotonic() - started) * 1000), "output_hash": _hash(reply.content)})
            parsed = parser(_json(reply.content, stage))
            receipt.update({"status": "succeeded", "artifact_validated": True}); self._record_receipt(receipt, stage_dir)
            return self._save_stage(stage, input_hash, parsed, attempt)
        except Exception as exc:
            receipt.update({"status": "failed", "artifact_validated": False, "error": str(exc)[:500], "duration_ms": int((time.monotonic() - started) * 1000)})
            self._record_receipt(receipt, stage_dir); self._failed_stage(stage, input_hash, attempt, exc); raise

    def _evidence_root(self, artifacts: Sequence[str]) -> Path:
        try: return Path(os.path.commonpath([str(self.artifact_root.resolve())] + [str(Path(x).expanduser().resolve()) for x in artifacts if x]))
        except ValueError: return self.artifact_root.parent.resolve()

    def _compile_evidence(self, kwargs: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]):
        primary, verification_batch, baseline_batch = kwargs.get("primary_batch", {}), kwargs.get("verification_batch"), kwargs.get("comparison_baseline_batch")
        paths = kwargs.get("path_specs") or {}
        evalpack_ref = kwargs.get("evalpack_ref")
        def rows(batch): return {str(x.get("case_id")): x for x in (batch.get("cases", ()) if isinstance(batch, Mapping) else ()) if isinstance(x, Mapping)}
        primary_rows, verification_rows, baseline_rows = rows(primary), rows(verification_batch), rows(baseline_batch)
        evidence, verification, baseline, artifacts = [], [], [], []
        for case in cases:
            cid = str(case.get("id")); item = compact_case_evidence(case, primary_rows.get(cid, {}), max_chars=self.max_evidence_chars_per_case, path_spec=paths.get(cid), evalpack_ref=evalpack_ref); evidence.append(item)
            if item.get("artifact"): artifacts.append(str(item["artifact"]))
            if verification_batch is not None: verification.append(compact_case_evidence(case, verification_rows.get(cid, {}), max_chars=self.max_evidence_chars_per_case, path_spec=paths.get(cid), evalpack_ref=evalpack_ref))
            if baseline_batch is not None: baseline.append(compact_case_evidence(case, baseline_rows.get(cid, {}), max_chars=self.max_evidence_chars_per_case, path_spec=None, evalpack_ref=evalpack_ref))
        self.evidence_query_port = LocalEvidenceQueryPort(self._evidence_root(artifacts))
        def heur(items): return {str(c.get("id")): v for c, i in zip(cases, items) if (v := _heuristic_case_result(c, i)) is not None}
        bundle = {"api_version": EVIDENCE_BUNDLE_API_VERSION, "task_id": kwargs.get("task_id"), "iteration": kwargs.get("iteration"), "environment_contract_hash": primary.get("environment_contract_hash"), "cases": evidence, "verification_evidence": verification, "without_skill_baseline_evidence": baseline, "context_compiler": "bounded-indexed-window/v2"}
        return bundle, heur(evidence), heur(verification), heur(baseline)

    def analyze(self, **kwargs: Any) -> Mapping[str, Any]:
        cases = tuple(x for x in kwargs.get("cases", ()) if isinstance(x, Mapping)); bundle, heuristic, verification_heuristic, baseline_heuristic = self._compile_evidence(kwargs, cases)
        verification_batch, baseline_batch = kwargs.get("verification_batch"), kwargs.get("comparison_baseline_batch")
        unresolved = [str(c.get("id")) for c in cases if str(c.get("id")) not in heuristic]
        baseline_unresolved = [str(c.get("id")) for c in cases if baseline_batch is not None and str(c.get("id")) not in baseline_heuristic]
        verification_unresolved = [str(c.get("id")) for c in cases if verification_batch is not None and str(c.get("id")) not in verification_heuristic]
        # The complete trace is relevant for every Case, including an exact
        # deterministic pass.  Keep the immutable artifact as the source of
        # truth and attach one bounded indexed window per Case for semantic
        # review; large logs remain queryable by artifact reference.
        selected = {str(c.get("id")) for c in cases if c.get("id")} | set(verification_unresolved) | set(baseline_unresolved)
        windows = []
        for item in bundle["cases"]:
            cid = str(item.get("case_id"))
            if cid not in selected or not item.get("artifact"): continue
            try:
                window = self.evidence_query_port.read_log_window(str(item["artifact"]), 0, min(2048, self.evidence_query_port.max_bytes)); self._record_query("semantic_grading", cid, str(item["artifact"]), window, None); windows.append({"case_id": cid, "window": window})
            except EvidenceQueryError as exc: self._record_query("semantic_grading", cid, str(item.get("artifact")), None, str(exc))
        # Receipt history is append-only provenance and must not alter the
        # content identity used for stage reuse after a process restart.
        bundle["on_demand_windows"], bundle["query_receipts"] = windows, list(self.current_query_receipts); bundle["input_hash"] = _hash(bundle)
        evidence_input = {"task_id": bundle["task_id"], "iteration": bundle["iteration"], "bundle": bundle}; evidence_hash = _hash(evidence_input)
        evidence = self._load_stage("evidence_compilation", evidence_hash, lambda x: x)
        if evidence is None:
            self._attempt_counter += 1; evidence = self._save_stage("evidence_compilation", evidence_hash, bundle, self._attempt_counter)
        else: bundle = evidence
        _write(self.artifact_root / "analysis-state.json", {"stage": "evidence_ready", "evidence_hash": bundle["input_hash"]})
        # Every Case gets an independent semantic review.  A deterministic
        # pass is not a reason to skip analysis: the reviewer must still
        # inspect the complete session trace against the Case goal,
        #重点检查项 and scoring dimensions so the UI can explain *why* it
        # passed and keep a regression baseline for later rounds.
        semantic_ids = sorted(str(c.get("id")) for c in cases if c.get("id"))
        semantic_input = {
            "api_version": SEMANTIC_VERDICT_API_VERSION,
            "skill_resources": kwargs.get("skill_resources", {}),
            "cases": [_model_compact(dict(c), max_string=1200) for c in cases if str(c.get("id")) in set(semantic_ids) | set(baseline_unresolved)],
            "evidence": [_model_case_context(x) for x in bundle["cases"] if str(x.get("case_id")) in set(semantic_ids)],
            "verification_evidence": [_model_case_context(x) for x in bundle["verification_evidence"] if str(x.get("case_id")) in set(semantic_ids)],
            "without_skill_baseline_evidence": [_model_case_context(x) for x in bundle["without_skill_baseline_evidence"] if str(x.get("case_id")) in set(baseline_unresolved)],
            "case_review_context": [
                _case_review_context(
                    case,
                    next((item for item in bundle["cases"] if str(item.get("case_id")) == str(case.get("id"))), {}),
                )
                for case in cases
                if str(case.get("id")) in set(semantic_ids)
            ],
            "windows": windows,
            "requested_case_ids": semantic_ids,
            "requested_baseline_case_ids": baseline_unresolved,
            "review_contract": {
                "case_goal": "逐 Case 对照 prompt、expected_observables 和通过标准核对结果",
                "scoring_dimensions": [
                    "outcome", "procedure", "grounding", "evidence_grounding",
                    "path", "binding", "runtime", "safety_side_effect",
                    "format", "efficiency", "reliability",
                ],
                "attribution": "明确区分 Skill 缺陷、Agent/模型执行偏差、远端工具/环境问题；不能把环境问题写成 Skill 失败",
                "trace": "完整日志已冻结在 evidence artifact；必须引用 artifact 或 trace event 位置，不得只看最终回复",
            },
        }
        semantic_input = _model_compact(semantic_input, max_string=1200)
        # Keep the Skill snapshot at a useful size for reasoning.  Evidence
        # strings are aggressively compacted, but truncating the Skill to the
        # same 1200-character limit can hide the very rule that explains a
        # failure.  ``_model_stage`` still applies the global prompt budget
        # and will compact this field only when the whole request is too large.
        if isinstance(kwargs.get("skill_resources"), Mapping):
            semantic_input["skill_resources"] = _model_compact(kwargs.get("skill_resources"), max_string=16_000)
        semantic_input_hash = _hash(semantic_input)
        if semantic_ids and self.model is not None:
            # Semantic grading is the evidence-heavy stage.  Split by Case
            # when needed, retaining all fields for each Case in its own
            # request.  The archived evidence bundle is never split or
            # truncated; only the model transport envelope is partitioned.
            semantic_parser = lambda value: _parse_semantic(value, semantic_ids, baseline_unresolved)
            semantic = self._load_stage("semantic_grading", semantic_input_hash, semantic_parser)
            if semantic is None:
                # Never combine independent Cases into one semantic call.  It
                # makes each conclusion auditable and prevents a verbose Case
                # from crowding another Case out of the model context.  The
                # cross-Case attribution/proposal stages below are the only
                # calls that intentionally reason over the suite as a whole.
                chunks: list[list[str]] = [[case_id] for case_id in sorted(set(semantic_ids) | set(baseline_unresolved))]
                semantic_rows: list[Mapping[str, Any]] = []
                baseline_rows: list[Mapping[str, Any]] = []
                for chunk in chunks:
                    chunk_ids = set(chunk)
                    chunk_input = dict(semantic_input)
                    chunk_input["cases"] = [item for item in semantic_input["cases"] if str(item.get("id")) in chunk_ids]
                    chunk_input["evidence"] = [item for item in semantic_input["evidence"] if str(item.get("case_id")) in chunk_ids]
                    chunk_input["verification_evidence"] = [item for item in semantic_input["verification_evidence"] if str(item.get("case_id")) in chunk_ids]
                    chunk_input["without_skill_baseline_evidence"] = [item for item in semantic_input["without_skill_baseline_evidence"] if str(item.get("case_id")) in chunk_ids]
                    chunk_input["case_review_context"] = [item for item in semantic_input["case_review_context"] if str(item.get("case_id")) in chunk_ids]
                    chunk_case_ids = [item for item in semantic_ids if item in chunk_ids]
                    chunk_baseline_ids = [item for item in baseline_unresolved if item in chunk_ids]
                    chunk_input["requested_case_ids"] = chunk_case_ids
                    chunk_input["requested_baseline_case_ids"] = chunk_baseline_ids
                    try:
                        chunk_semantic = self._model_stage(
                            "semantic_grading",
                            chunk_input,
                            lambda value, ids=chunk_case_ids, baseline_ids=chunk_baseline_ids: _parse_semantic(value, ids, baseline_ids),
                        )
                    except (ClaudeProfileError, ReferenceRuntimeError) as exc:
                        # A transient upstream 502 must not discard the
                        # already-frozen execution evidence. Continue with a
                        # deterministic per-Case verdict and expose the model
                        # failure as analysis metadata instead of forcing a
                        # full evaluation rerun.
                        chunk_semantic = {
                            "case_results": [{"case_id": cid, "status": heuristic.get(cid, {}).get("status", "fail"), "reason": "语义模型暂时不可用，已依据冻结执行事实完成分析：%s" % str(exc)[:240], "evidence_refs": heuristic.get(cid, {}).get("evidence_refs", [])} for cid in chunk_case_ids],
                            "without_skill_baseline_case_results": [],
                        }
                    semantic_rows.extend(chunk_semantic["case_results"])
                    baseline_rows.extend(chunk_semantic["without_skill_baseline_case_results"])
                semantic = {"case_results": semantic_rows, "without_skill_baseline_case_results": baseline_rows}
                # A single chunk already has the full input identity and was
                # persisted by _model_stage.  Only write an aggregate alias
                # when multiple chunk artifacts need to be joined.
                if len(chunks) > 1:
                    self._attempt_counter += 1
                    self._save_stage("semantic_grading", semantic_input_hash, semantic, self._attempt_counter)
            heuristic.update({str(x["case_id"]): dict(x) for x in semantic["case_results"]}); baseline_heuristic.update({str(x["case_id"]): dict(x) for x in semantic["without_skill_baseline_case_results"]})
        elif semantic_ids:
            # A missing semantic model must not manufacture an "evidence
            # insufficient" Case when the immutable run already proves that
            # the Agent loaded the Skill and completed the task.  Deterministic
            # hard facts remain the verdict; the UI marks the semantic layer
            # as unavailable while still providing a scored Case outcome.
            semantic_rows = [{"case_id": cid, "status": heuristic.get(cid, {}).get("status", "fail"), "verification_status": "not_run", "reason": "基于冻结执行事实完成评分；未配置语义分析模型", "evidence_refs": heuristic.get(cid, {}).get("evidence_refs", [])} for cid in semantic_ids]
            baseline_rows = [{"case_id": cid, "status": baseline_heuristic.get(cid, {}).get("status", "fail"), "reason": "基于冻结执行事实完成基线评分；未配置语义分析模型", "evidence_refs": baseline_heuristic.get(cid, {}).get("evidence_refs", [])} for cid in baseline_unresolved]
            semantic = {"case_results": semantic_rows, "without_skill_baseline_case_results": baseline_rows}
            heuristic.update({str(x["case_id"]): dict(x) for x in semantic_rows}); baseline_heuristic.update({str(x["case_id"]): dict(x) for x in baseline_rows})
            self._attempt_counter += 1; self._save_stage("semantic_grading", semantic_input_hash, semantic, self._attempt_counter)
        else:
            semantic = {"case_results": [], "without_skill_baseline_case_results": []}; self._save_stage("semantic_grading", semantic_input_hash, semantic, self._attempt_counter)
        # A valid completed session is always an analyzable observation.  If a
        # semantic judge answers ``not_evaluable`` despite a complete artifact
        # and Trace, retain its explanation as a hypothesis but materialize a
        # normal failed outcome so the Case enters diagnosis/optimization
        # instead of an endless evidence-retry loop.
        evidence_by_id = {str(item.get("case_id")): item for item in bundle.get("cases", ()) if isinstance(item, Mapping)}
        for cid, value in list(heuristic.items()):
            if value.get("status") != "not_evaluable":
                continue
            item = evidence_by_id.get(cid, {})
            trace = item.get("trace") if isinstance(item.get("trace"), Mapping) else {}
            if item.get("artifact") and item.get("trace_complete") is True and int(trace.get("event_count") or 0) > 0:
                updated = dict(value)
                updated["status"] = "fail"
                updated["analysis_status"] = "not_evaluable"
                updated["reason"] = str(value.get("reason") or "语义模型未给出通过结论") + "；会话证据完整，按可分析的目标偏差进入归因"
                heuristic[cid] = updated
        results_by_id = {str(c.get("id")): heuristic[str(c.get("id"))] for c in cases}
        # Re-apply frozen deterministic facts after semantic grading.
        for cid, value in self._hard_results(cases, bundle["cases"]).items(): results_by_id[cid] = value
        inventory = list(kwargs.get("editable_resource_inventory") or ("SKILL.md",)); failures = [cid for cid, x in results_by_id.items() if x.get("status") == "fail"]
        # Give cross-Case attribution the deterministic dimension evidence as
        # well as the semantic rows.  Previously this context was only
        # materialized later in ``_assemble``; the model therefore saw Case
        # IDs and short reasons but not which concrete dimensions failed.
        preliminary_results = [results_by_id[str(case.get("id"))] for case in cases if str(case.get("id")) in results_by_id]
        preliminary_verification = {
            str(case.get("id")): verification_heuristic[str(case.get("id"))]
            for case in cases
            if str(case.get("id")) in verification_heuristic
        }
        preliminary_aggregates = build_case_aggregates(
            cases,
            preliminary_results,
            list(bundle.get("cases", ())),
            list(bundle.get("verification_evidence", ())),
            verification_results=preliminary_verification,
            required_k=self.required_k,
            run_context={"task_id": kwargs.get("task_id"), "iteration": kwargs.get("iteration")},
        )
        # Keep this cross-case projection intentionally small.  The complete
        # attempt records are archived and are still available through the
        # bounded evidence port; the attribution/proposal model only needs the
        # failed dimensions and their concrete reason to compare Cases.  A
        # previous version copied every attempt here, which made the two
        # downstream stages exceed their prompt budget for a multi-Case run.
        deterministic_case_context = []
        for aggregate in preliminary_aggregates:
            failed_dimensions = []
            for attempt in aggregate.attempts:
                for dimension in attempt.dimensions:
                    if dimension.status not in {"fail", "not_evaluable"}:
                        continue
                    failed_dimensions.append({
                        "dimension": dimension.dimension,
                        "label": _dimension_label_cn(dimension.dimension),
                        "status": dimension.status,
                        "score": dimension.score,
                        "reason": str(dimension.reason or "")[:360],
                        "evidence_refs": list(dimension.evidence_refs)[:4],
                    })
            deterministic_case_context.append({
                "case_id": aggregate.case_id,
                "status": aggregate.status,
                "stable_pass": aggregate.stable_pass,
                "failed_dimensions": failed_dimensions[:8],
            })
        attribution_input = _model_compact({"api_version": ATTRIBUTION_REPORT_API_VERSION, "evidence_hash": bundle["input_hash"], "skill_resources": kwargs.get("skill_resources", {}), "semantic_verdict": semantic, "case_results": list(results_by_id.values()), "cases": [dict(c) for c in cases], "case_review_context": semantic_input.get("case_review_context", []), "verification_evidence": list(bundle["verification_evidence"]), "editable_resource_inventory": inventory, "instructions": "跨 Case 汇总前先保留每个 Case 独立结论；只有多个 Case 的失败维度/证据指向同一 Skill 机制时才聚类，明确潜在冲突。必须引用 Skill 原文中的具体规则/段落，说明哪些失败是 Skill 缺口，哪些是 Agent/模型执行偏差，哪些是工具/环境或评测标准问题；不得只罗列 Case 名称或日志路径。"})
        attribution_input["deterministic_case_context"] = deterministic_case_context
        # Re-compact after adding the cross-case projection.  Stage inputs are
        # hashed before transport, but the payload itself must fit the local
        # model's prompt budget.
        attribution_input = _model_compact(attribution_input, max_string=1200)
        if isinstance(kwargs.get("skill_resources"), Mapping):
            attribution_input["skill_resources"] = _model_compact(kwargs.get("skill_resources"), max_string=16_000)
        if self.model is not None and (failures or semantic_ids):
            try:
                attribution = self._model_stage("attribution", attribution_input, _parse_attribution)
            except (ClaudeProfileError, ReferenceRuntimeError):
                attribution = {"failure_clusters": ([{"id": "observed-failures", "case_ids": failures, "root_cause": "根据冻结执行事实，失败 Case 未满足 Skill 要求；需由用户确认最小 Skill 修改", "skill_change_authorized": bool(failures)}] if failures else []), "conflicts": []}
        else:
            attribution = {"failure_clusters": ([{"id": "observed-failures", "case_ids": failures, "root_cause": "requires cross-case Skill repair analysis", "skill_change_authorized": bool(failures)}] if failures else []), "conflicts": []}; self._save_stage("attribution", _hash(attribution_input), attribution, self._attempt_counter)
        proposal_input = _model_compact({"api_version": OPTIMIZATION_PLAN_API_VERSION, "evidence_hash": bundle["input_hash"], "skill_resources": kwargs.get("skill_resources", {}), "semantic_verdict": semantic, "attribution_report": attribution, "cases": [dict(c) for c in cases], "case_review_context": semantic_input.get("case_review_context", []), "editable_resource_inventory": inventory, "instructions": "提出最小、可验证且不让一个 Case 的修复破坏另一个 Case 的修改；必须针对 Skill 原文给出具体章节/规则改法、修改后应新增的行为约束或自检清单；列出受影响 Case、保护 Case、验证步骤和可能冲突。禁止使用‘根据失败维度修改 Skill’‘保留通过 Case’等无信息量句子。"})
        proposal_input["deterministic_case_context"] = deterministic_case_context
        proposal_input = _model_compact(proposal_input, max_string=1200)
        if isinstance(kwargs.get("skill_resources"), Mapping):
            proposal_input["skill_resources"] = _model_compact(kwargs.get("skill_resources"), max_string=16_000)
        if self.model is not None and (failures or semantic_ids):
            try:
                proposal = self._model_stage("proposal", proposal_input, lambda x: _parse_proposal(x, inventory))
            except (ClaudeProfileError, ReferenceRuntimeError):
                proposal = {"proposed_changes": ([{"target": "SKILL.md", "change": "根据逐 Case 失败事实补充明确的执行步骤、产出要求和自检标准", "why": "分析模型暂时不可用，但冻结执行证据显示目标未满足", "case_ids": failures}] if failures else []), "target_scope": ["SKILL.md"] if failures else []}
        else:
            proposal = {"proposed_changes": ([{"target": "SKILL.md", "change": "repair the shared cause supported by failed case evidence", "why": "one or more frozen expectations failed", "case_ids": failures}] if failures else []), "target_scope": ["SKILL.md"] if failures else []}; self._save_stage("proposal", _hash(proposal_input), proposal, self._attempt_counter)
        _write(self.artifact_root / "analysis-state.json", {"stage": "proposal", "evidence_hash": bundle["input_hash"], "completed": True})
        return self._assemble(kwargs, cases, bundle, results_by_id, verification_heuristic, baseline_heuristic, attribution, proposal)

    @staticmethod
    def _hard_results(cases, evidence):
        out = {}
        for case, item in zip(cases, evidence):
            value = _heuristic_case_result(case, item)
            if value is None:
                continue
            # Only frozen, deterministic facts may override a model's
            # semantic verdict.  An open-ended Case receives a provisional
            # execution-fact result from _heuristic_case_result so it remains
            # scoreable without a model, but its final status belongs to the
            # independent Case analysis call.
            path = item.get("path_conformance") if isinstance(item.get("path_conformance"), Mapping) else {}
            formal = item.get("formal_grading") if isinstance(item.get("formal_grading"), Mapping) else {}
            exact = item.get("exact_expected_match")
            authoritative = isinstance(exact, bool) or path.get("status") == "fail" or formal.get("status") in ("pass", "fail")
            if authoritative:
                out[str(case.get("id"))] = value
        return out

    def _assemble(self, kwargs, cases, bundle, results_by_id, verification_heuristic, baseline_heuristic, attribution, proposal):
        task_id = str(kwargs.get("task_id"))
        iteration = int(kwargs.get("iteration", 0))
        evidence = list(bundle["cases"])
        results = [dict(results_by_id[str(case.get("id"))]) for case in cases]
        eligible = {
            str(case.get("id"))
            for case in cases
            if _optimization_case_ready(case)
        }
        inventory = set(
            str(item)
            for item in (kwargs.get("editable_resource_inventory") or ("SKILL.md",))
        )

        primary_passes = [item["case_id"] for item in results if item["status"] == "pass"]

        # Build the deterministic aggregate before deriving stability and
        # authorization lists. A model-provided ``verification_status`` is
        # only a hint; hard dimensions and evidence validity remain decisive.
        verification_results = {
            str(case.get("id")): verification_heuristic[str(case.get("id"))]
            for case in cases
            if str(case.get("id")) in verification_heuristic
        }
        if kwargs.get("verification_batch") is not None:
            for case in cases:
                case_id = str(case.get("id"))
                if case_id in verification_results:
                    continue
                semantic_status = results_by_id.get(case_id, {}).get("verification_status")
                if semantic_status in ("pass", "fail", "not_evaluable"):
                    verification_results[case_id] = {
                        "case_id": case_id,
                        "status": semantic_status,
                        "reason": "local semantic grader stability verdict: %s" % semantic_status,
                        "evidence_refs": results_by_id.get(case_id, {}).get("evidence_refs", ()),
                    }
        aggregates = build_case_aggregates(
            cases,
            results,
            evidence,
            list(bundle.get("verification_evidence", [])),
            verification_results=verification_results,
            required_k=self.required_k,
            run_context={
                "task_id": task_id,
                "comparison_context_hash": kwargs.get("comparison_context_hash"),
                "evaluation_design_hash": kwargs.get("evaluation_design_hash"),
            },
        )
        aggregate_by_id = {item.case_id: item for item in aggregates}
        verification_ids = {
            str(item.get("case_id"))
            for item in bundle.get("verification_evidence", ())
            if isinstance(item, Mapping) and item.get("case_id")
        }
        if kwargs.get("verification_batch") is not None:
            stable = [
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
            stable = []
            flaky = []

        failed = [item["case_id"] for item in results if item["status"] == "fail"] + flaky
        authorizable_failure_ids = set(failed).intersection(eligible)

        # A model may explain or group any observed row, but only a real
        # failed/flaky Case with a trusted Oracle may authorize a Skill edit.
        clusters = []
        for cluster in attribution.get("failure_clusters", []):
            value = dict(cluster)
            original = [str(item) for item in value.get("case_ids", [])]
            authorized_ids = [item for item in original if item in authorizable_failure_ids]
            value["case_ids"] = authorized_ids
            value["excluded_unready_case_ids"] = [item for item in original if item not in eligible]
            value["excluded_non_failure_case_ids"] = [
                item for item in original if item in eligible and item not in authorizable_failure_ids
            ]
            # Trusted deterministic Case eligibility is the mutation gate. A
            # model may explicitly veto a Skill attribution, but omitting the
            # redundant cluster boolean must not hide a real authorized
            # failure from the diagnosis/optimization panels.
            value["skill_change_authorized"] = bool(
                value.get("skill_change_authorized") is not False and authorized_ids
            )
            clusters.append(value)

        changes = []
        for change in proposal.get("proposed_changes", []):
            value = dict(change)
            original = [str(item) for item in value.get("case_ids", [])]
            authorized_ids = [item for item in original if item in authorizable_failure_ids]
            if not authorized_ids:
                continue
            value["case_ids"] = authorized_ids
            value["excluded_unready_case_ids"] = [item for item in original if item not in eligible]
            value["excluded_non_failure_case_ids"] = [
                item for item in original if item in eligible and item not in authorizable_failure_ids
            ]
            changes.append(value)

        # A schema-valid proposal may still omit every Case (for example a
        # model returned a generic proposal without copying case_ids).  Do not
        # leave the reviewer with a conclusion but no actionable next step:
        # preserve the evidence boundary and create a conservative fallback
        # proposal scoped to the existing Skill resource.
        if authorizable_failure_ids and not changes:
            changes.append({
                "target": "SKILL.md",
                "change": "根据失败维度和目标核对结果修正 Skill，并保留通过 Case 作为回归保护",
                "why": "一个或多个可信失败 Case 已绑定不可变会话证据",
                "case_ids": sorted(authorizable_failure_ids),
            })

        eligible_flaky = [case_id for case_id in flaky if case_id in eligible]
        if eligible_flaky:
            clusters.append({
                "id": "unstable-pass-verification",
                "case_ids": eligible_flaky,
                "excluded_unready_case_ids": [],
                "excluded_non_failure_case_ids": [],
                "root_cause": "a trusted primary pass did not reproduce in verification",
                "skill_change_authorized": True,
            })
            changes.append({
                "target": "SKILL.md",
                "change": "make the affected workflow deterministic",
                "why": "a trusted primary pass was not stable",
                "case_ids": eligible_flaky,
            })

        requested_scope = [str(item) for item in proposal.get("target_scope", [])]
        if eligible_flaky and "SKILL.md" not in requested_scope:
            requested_scope.append("SKILL.md")
        for change in changes:
            target = str(change.get("target") or "")
            if target and target not in requested_scope:
                requested_scope.append(target)
        unsupported_scope = [item for item in requested_scope if item not in inventory]
        scope = [item for item in requested_scope if item in inventory]

        not_eval = sorted(set(
            [item["case_id"] for item in results if item["status"] == "not_evaluable"]
            + [item.case_id for item in aggregates if item.status == "not_evaluable"]
        ))
        result_by_id = {str(item.get("case_id")): item for item in results}
        evidence_issues = [
            _evidence_issue(case_id, result_by_id.get(case_id, {}), aggregate_by_id.get(case_id))
            for case_id in not_eval
        ]
        issue_by_id = {str(item.get("case_id")): item for item in evidence_issues}
        case_assessments = []
        for case in cases:
            case_id = str(case.get("id"))
            result = result_by_id.get(case_id, {"case_id": case_id, "status": "not_evaluable", "reason": "missing case result"})
            aggregate = aggregate_by_id.get(case_id)
            aggregate_status = aggregate.status if aggregate is not None else str(result.get("status") or "not_evaluable")
            issue = issue_by_id.get(case_id)
            raw_result_status = str(result.get("status") or "not_evaluable")
            pending_verification = bool(
                aggregate is not None
                and aggregate_status == "fail"
                and raw_result_status == "pass"
                and not aggregate.flaky
                and not aggregate.stable_pass
            )
            if aggregate_status == "not_evaluable":
                responsibility = "evidence_gap"
                conclusion = "当前不能判断 Skill 是否通过；这不是 Skill 失败。"
                recommended_action = str(issue.get("guidance") if issue else "补齐该 Case 的证据或判定依据后重新评测。")
                skill_recommendation = "暂不修改 Skill；先补齐可核验证据或可信通过标准。"
                fixture_recommendation = (
                    "修正该 Case 的测试分支 base/head commit 和绑定收据后重试。"
                    if issue and issue.get("category") == "repository_binding"
                    else "测试分支不一定有问题；先按证据缺口说明定向重试或补充日志。"
                )
            elif pending_verification:
                responsibility = "pass_pending_verification"
                conclusion = "本次尝试各维度均通过，但尚未达到稳定性复验次数；不能据此判定 Skill 已稳定通过。"
                recommended_action = "继续执行稳定性复验；不要修改 Skill，也不要重建测试分支，除非绑定或日志核验失败。"
                skill_recommendation = "暂不修改 Skill；当前没有已证实的 Skill 缺陷。"
                fixture_recommendation = "保持现有测试分支和 commit 绑定，优先复验同一分支。"
            elif aggregate_status == "fail" and case_id in authorizable_failure_ids:
                responsibility = "skill_improvement_candidate"
                conclusion = "可信评测未达标，可作为 Skill 改进候选；仍需通过下一轮回归验证归因。"
                recommended_action = "检查下方根因假设与最小修改提案，确认后生成 Skill 候选并使用同一批 Case 回归。"
                skill_recommendation = "根据失败维度和根因假设修改 Skill；保留通过 Case 作为回归保护。"
                fixture_recommendation = "当前证据已足以评估测试分支；除非发现代码改动或 commit 绑定错误，否则不应修改 Fixture。"
            elif aggregate_status == "fail":
                responsibility = "failure_not_authorized"
                conclusion = "观察到未通过或不稳定，但当前判定依据不足以直接归因并修改 Skill。"
                recommended_action = "先校准 Case 的通过标准、Fixture 或证据绑定，再决定是否修改 Skill。"
                skill_recommendation = "暂不直接修改 Skill；先完成 Oracle、日志或归因校准。"
                fixture_recommendation = "核对测试分支是否覆盖 Case 触发条件、base/head commit 和仓库绑定；必要时只重建该 Fixture。"
            else:
                responsibility = "stable_pass" if aggregate is not None and aggregate.stable_pass else "pass_pending_verification"
                conclusion = "该 Case 当前通过。" if responsibility == "stable_pass" else "该 Case 初测通过，仍需完成稳定性复验。"
                recommended_action = "保持该能力，并在后续候选回归中作为保护 Case。"
                skill_recommendation = "暂不修改 Skill；将此 Case 作为回归保护。"
                fixture_recommendation = "保持当前测试分支和版本绑定。"
            evidence_item = next((item for item in evidence if str(item.get("case_id")) == case_id), {})
            attempts = _enrich_attempt_dimensions(
                [attempt.to_dict() for attempt in aggregate.attempts] if aggregate is not None else [],
                evidence_item,
                case,
            )
            goal_observations = _case_goal_observations(
                case,
                evidence_item,
                result,
                aggregate,
                issue,
            )
            dimension_summaries = _dimension_summaries(attempts)
            for summary in dimension_summaries:
                summary["evidence_detail"] = _dimension_evidence_detail(summary, evidence_item, case)
            hard_failures = [
                str(item.get("label") or _dimension_label_cn(item.get("dimension")))
                for item in dimension_summaries
                if item.get("hard") is True and item.get("status") == "fail"
            ]
            hard_gaps = [
                str(item.get("label") or _dimension_label_cn(item.get("dimension")))
                for item in dimension_summaries
                if item.get("hard") is True and item.get("status") == "not_evaluable"
            ]
            missing_goals = [str(item) for item in goal_observations.get("missing_requirements", ()) if str(item)]
            goal_total = int(goal_observations.get("total_count") or 0)
            goal_observed = int(goal_observations.get("observed_count") or 0)
            goal_line = (
                "目标逐条核对：%d/%d 项在输出或 Trace 中观察到" % (goal_observed, goal_total)
                if goal_total
                else "该 Case 没有声明可逐条核对的目标"
            )
            # The deterministic status remains authoritative; these additions
            # make its relationship to the user's Case objective explicit.
            if pending_verification:
                conclusion = "%s %s；当前只是初测通过，尚未完成稳定性复验。" % (conclusion, goal_line)
            elif aggregate_status == "fail" and case_id in authorizable_failure_ids:
                detail = "；硬门未通过：%s" % "、".join(hard_failures) if hard_failures else "；结果或执行路径未达到通过标准"
                if missing_goals:
                    detail += "；" + goal_line + "，未观察到：%s" % "、".join(missing_goals[:3])
                conclusion = "Case 未通过：可信会话证据显示 Skill 尚未满足该 Case 的要求%s。该结论可作为 Skill 改进候选，但修改后仍需回归验证。" % detail
            elif aggregate_status == "fail" and case_id not in authorizable_failure_ids:
                detail = "；硬门证据缺口：%s" % "、".join(hard_gaps) if hard_gaps else "；当前判定依据不能安全归因"
                conclusion = "Case 未通过，但不能直接归因到 Skill%s。先补齐通过标准、仓库绑定或完整日志，再决定修改方向。" % detail
            elif aggregate_status == "pass":
                conclusion = "%s %s。" % (conclusion.rstrip("。"), goal_line)
            skill_factors = _text_list(result.get("skill_factors", result.get("skill_issue", [])))
            agent_model_factors = _text_list(result.get("agent_model_factors", result.get("model_factors", [])))
            environment_factors = _text_list(result.get("environment_factors", result.get("tool_factors", [])))
            failed_dimension_labels = [
                str(item.get("label") or _dimension_label_cn(item.get("dimension")))
                for item in dimension_summaries
                if item.get("status") == "fail"
            ]
            if responsibility == "skill_improvement_candidate" and not skill_factors:
                skill_factors = [
                    "Skill 未能稳定满足%s" % ("、".join(failed_dimension_labels[:4]) if failed_dimension_labels else "该 Case 的目标与执行要求")
                ]
            if responsibility in {"failure_not_authorized", "evidence_gap"} and not environment_factors:
                environment_factors = [
                    str(issue.get("reason_cn") if issue else "当前证据、Oracle 或测试分支绑定不足，无法安全归因到 Skill")
                ]
            if any(item.get("status") == "fail" and _dimension_base(item.get("dimension")) == "runtime" for item in dimension_summaries) and not agent_model_factors:
                agent_model_factors = ["Trace 记录了运行时/工具错误，需确认 Agent 执行偏差或远端工具故障"]
            case_assessments.append({
                "case_id": case_id,
                "status": aggregate_status,
                "display_status": "pending_verification" if pending_verification else aggregate_status,
                "semantic_status": str(result.get("status") or "not_evaluable"),
                "reason": str(result.get("reason") or "没有形成结论说明"),
                "attribution": responsibility,
                # Preserve the model's causal split as a hypothesis. The
                # deterministic responsibility above remains the authority
                # for mutation permissions.
                "attribution_hypothesis": result.get("attribution") or result.get("responsibility") or result.get("cause_type"),
                "skill_factors": skill_factors,
                "agent_model_factors": agent_model_factors,
                "environment_factors": environment_factors,
                "attribution_confidence": result.get("attribution_confidence", result.get("confidence")),
                "conclusion": conclusion,
                "recommended_action": recommended_action,
                "skill_recommendation": skill_recommendation,
                "fixture_recommendation": fixture_recommendation,
                "pending_verification": pending_verification,
                "evidence_issue": dict(issue) if issue else None,
                "attempts": attempts,
                "timing": next((item.get("timing", {}) for item in evidence if str(item.get("case_id")) == case_id), {}),
                "retry_count": next((item.get("retry_count", 0) for item in evidence if str(item.get("case_id")) == case_id), 0),
                "dimensions": [dimension for attempt in attempts for dimension in attempt.get("dimensions", ())],
                "dimension_summaries": dimension_summaries,
                "hard_failed_dimensions": hard_failures,
                "hard_evidence_gaps": hard_gaps,
                "goal_completion": {
                    "observed": goal_observed,
                    "total": goal_total,
                    "missing": missing_goals,
                    "status": "complete" if goal_total and not missing_goals else "incomplete" if goal_total else "not_declared",
                },
                "goal_observations": goal_observations,
            })
        if primary_passes and kwargs.get("verification_batch") is None:
            next_action = "verify_passes"
        elif not_eval:
            next_action = "needs_evidence"
        elif failed:
            next_action = "await_user_confirmation"
        else:
            next_action = "converged"

        blocker = None
        if next_action == "await_user_confirmation":
            # ``authorizable_failure_ids`` is the single authorization source:
            # it already requires a trusted Oracle, a real failed/flaky Case,
            # and an editable target.  Requiring the model to also echo a
            # cluster-level boolean caused a dead-end when it returned valid
            # failure evidence and proposals but omitted that redundant flag.
            if not authorizable_failure_ids:
                next_action = "needs_evidence"
                if flaky and not eligible_flaky:
                    blocker = "unstable Cases are not Oracle-ready and cannot authorize a Skill change"
                else:
                    blocker = "no trusted failed Case authorizes a Skill change"
            elif unsupported_scope:
                next_action = "needs_evidence"
                blocker = "target scope is not an editable existing Skill resource: %s" % ", ".join(unsupported_scope)
        overall_assessment = _overall_assessment(
            cases,
            case_assessments,
            clusters,
            changes,
            evidence_issues,
            next_action,
        )
        diagnosis = compile_diagnosis_graph(
            case_assessments,
            clusters,
            changes,
            list(attribution.get("conflicts", [])),
        )
        model_calls = sum(1 for item in self.receipts if item.get("status") == "succeeded")
        usage = {}
        for receipt in self.receipts:
            for key, value in (receipt.get("usage") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    usage[key] = usage.get(key, 0) + value
        receipt_paths = sorted(str(path) for path in self.artifact_root.glob("attempt-*/**/agent-call-receipt.json"))
        root_receipt_paths = sorted(str(path) for path in self.artifact_root.glob("agent-call-receipt-*.json"))
        query_paths = sorted(str(path) for path in self.artifact_root.glob("evidence-query-receipt-*.json"))
        return {
            "api_version": ANALYSIS_DECISION_API_VERSION,
            "task_id": task_id,
            "iteration": iteration,
            "brain_stage": "proposal",
            "case_results": results,
            "stable_pass_case_ids": stable,
            "verification_required_case_ids": primary_passes if kwargs.get("verification_batch") is None else [],
            "flaky_case_ids": flaky,
            "failed_case_ids": failed,
            "not_evaluable_case_ids": not_eval,
            "evidence_issues": evidence_issues,
            "recovery": {
                "full_restart_required": False,
                "targeted_retry_case_ids": [item["case_id"] for item in evidence_issues if item.get("targeted_retry_available")],
                "reopen_case_review_case_ids": [item["case_id"] for item in evidence_issues if item.get("recommended_action") == "reopen_case_review"],
            },
            "case_assessments": case_assessments,
            "overall_assessment": overall_assessment,
            "without_skill_baseline_case_results": [
                baseline_heuristic.get(
                    str(case.get("id")),
                    {
                        "case_id": str(case.get("id")),
                        "status": "not_evaluable",
                        "reason": "baseline requires semantic analysis",
                        "evidence_refs": [],
                    },
                )
                for case in cases
            ] if kwargs.get("comparison_baseline_batch") is not None else [],
            "incremental_value_case_ids": [
                item["case_id"]
                for item in results
                if item["status"] == "pass"
                and baseline_heuristic.get(item["case_id"], {}).get("status") == "fail"
            ],
            "failure_clusters": clusters,
            "conflicts": list(attribution.get("conflicts", [])),
            "proposed_changes": changes,
            "target_scope": scope if changes else [],
            "optimization_eligible_case_ids": sorted(eligible),
            "authorizable_failure_case_ids": sorted(authorizable_failure_ids),
            "next_action": next_action,
            "intervention_blocker": blocker,
            "requires_user_confirmation": next_action == "await_user_confirmation",
            "analysis_usage": usage,
            "attempt_verdicts": [attempt.to_dict() for aggregate in aggregates for attempt in aggregate.attempts],
            "case_aggregates": [aggregate.to_dict() for aggregate in aggregates],
            "diagnosis_graph": diagnosis,
            "evidence_health": {
                "valid_attempts": sum(attempt.evidence_validity.status == "valid" for aggregate in aggregates for attempt in aggregate.attempts),
                "invalid_attempts": sum(attempt.evidence_validity.status == "invalid" for aggregate in aggregates for attempt in aggregate.attempts),
                "not_evaluable_case_ids": [aggregate.case_id for aggregate in aggregates if aggregate.status == "not_evaluable"],
            },
            "token_economy": {
                "model_calls": model_calls,
                "full_logs_sent": False,
                "evidence_artifacts": [item.get("artifact") for item in evidence],
                "serialized_prompt_chars": self._prompt_chars,
                "estimated_prompt_tokens": (self._prompt_chars + 3) // 4,
            },
            "analysis_artifacts": {
                "evidence_bundle": str(self.artifact_root / "evidence-bundle.json"),
                "semantic_verdict": str(self.artifact_root / "semantic-verdict.json"),
                "attribution_report": str(self.artifact_root / "attribution-report.json"),
                "optimization_plan": str(self.artifact_root / "optimization-plan.json"),
                "agent_call_receipts": receipt_paths,
                "agent_call_receipt_history": root_receipt_paths,
                "evidence_query_receipts": query_paths,
            },
            "agent_call_receipts": list(self.receipts),
            "evidence_query_receipts": list(self.query_receipts),
        }


__all__ = ["IterationBrain", "LocalEvidenceQueryPort", "EvidenceQueryError"]
