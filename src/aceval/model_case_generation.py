"""Model-driven, source-grounded evaluation Case and Path generation.

The deterministic planner still owns requirements and runtime safety.  This
module only lets the configured local/CC Switch model turn those requirements
into readable, intent-specific prompts and execution paths.  Every model
claim is checked against the planner's requirement/source vocabulary before it
is accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence, Tuple

from .agent_runtime import ModelClient
from .test_planning import TestPlan


MODEL_CASE_DESIGN_API_VERSION = "aceval.model-case-design/v1"


class ModelCaseGenerationError(ValueError):
    pass


def _hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _text(value: Any, label: str, maximum: int = 12000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ModelCaseGenerationError("%s must be a non-empty bounded string" % label)
    return value.strip()


def _strings(value: Any, label: str, *, required: bool = True) -> Tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ModelCaseGenerationError("%s must be an array of strings" % label)
    result = tuple(str(item).strip() for item in value if isinstance(item, str) and item.strip())
    if required and not result:
        raise ModelCaseGenerationError("%s must not be empty" % label)
    return result


def _json_content(content: str) -> Mapping[str, Any]:
    raw = str(content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    # Claude/Codex occasionally prefixes a short explanation even when asked
    # for JSON only. Extract one balanced top-level object while preserving
    # strict JSON parsing and rejecting malformed model output.
    if not raw.startswith("{"):
        start = raw.find("{")
        if start >= 0:
            depth = 0
            in_string = False
            escaped = False
            end = None
            for index in range(start, len(raw)):
                char = raw[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end = index + 1
                        break
            if end is not None:
                raw = raw[start:end]
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ModelCaseGenerationError("model returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ModelCaseGenerationError("model case design must be an object")
    return value


def build_case_generation_prompt(
    *,
    skill_text: str,
    graph: Mapping[str, Any],
    plan: TestPlan,
    goal: str,
    standards: Sequence[str],
    seed_cases: Sequence[Mapping[str, Any]],
    target_case_ids: Sequence[str],
) -> str:
    requirements = [item.to_dict() for item in plan.requirements if item.id in {
        case.requirement_ids[0] for case in plan.cases if case.id in set(target_case_ids) and case.requirement_ids
    }]
    # Include the complete requirement list when it is small; otherwise keep
    # the prompt bounded while preserving all ids and source refs.
    if len(requirements) < len(plan.requirements):
        requirements = [item.to_dict() for item in plan.requirements]
    context = {
        "goal": goal,
        "standards": list(standards),
        "target_case_ids": list(target_case_ids),
        "requirements": requirements,
        "runtime_capabilities": list(plan.runtime_capabilities),
        "skill_graph": {
            "subject_hash": graph.get("subject_hash"),
            "source_path": graph.get("source_path"),
            "sections": graph.get("sections", []),
            "capabilities": graph.get("capabilities", []),
            "tools": graph.get("tools", []),
        },
        "seed_cases": list(seed_cases),
    }
    context_json = json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2)
    skill = skill_text[:24000]
    return (
        "你是 FORGE 的评测设计模型。根据给定 SKILL.md、能力图和确定性测试要求，"
        "为 target_case_ids 中每个 Case 生成一个不同测试意图的可执行评测。"
        "不要发明未出现在 requirements 或 SKILL.md 的事实；每个 source_refs 必须使用 requirements 中已有的 source_refs（如 cap.x#source-0）。"
        "Case 必须覆盖不同维度（happy_path、edge/negative、recovery、boundary、idempotency 等），"
        "prompt 必须写成真实用户任务而不是模板句。路径 steps 要描述可观测的读取、工具、输出和禁止动作。"
        "所有面向用户展示的字段必须使用简体中文，包括 title、prompt、family、generation_reason、expected_observables、"
        "oracle_strategy、path purpose 和 step label；文件名、工具名、Case/Requirement/source ref 等技术标识可保留原文。"
        "只返回严格 JSON，不要 Markdown 或代码围栏；JSON 字符串内部的双引号必须使用反斜杠转义，不能原样嵌入。"
        "每个 path step 的 match 只允许 event_type、tool_name、contains、fields 四种键，例如 "
        "{\"event_type\":\"agent.tool_use\",\"tool_name\":\"read_file\",\"contains\":\"SKILL.md\"}；"
        "禁止使用 tool、pattern、output、regex 等其它键。\n\n"
        "返回结构：{\"api_version\":\"aceval.model-case-design/v1\",\"cases\":["
        "{\"id\":...,\"title\":...,\"prompt\":...,\"family\":...,\"kind\":...,"
        "\"requirement_ids\":[...],\"source_refs\":[...],\"generation_reason\":...,"
        "\"expected_observables\":[...],\"oracle_strategy\":...,\"required_runtime_capabilities\":[...]}],"
        "\"paths\":[{\"case_id\":...,\"purpose\":...,\"steps\":["
        "{\"id\":...,\"label\":...,\"kind\":\"required|recommended|forbidden|alternative\",\"match\":{},\"after\":[]}]}],"
        "\"generation_summary\":{\"strategy\":...,\"distinct_intents\":[...]}}\n\n"
        "SKILL.md（来源依据）：\n%s\n\n确定性上下文：\n%s" % (skill, context_json)
    )


def build_case_generation_repair_prompt(
    *,
    validation_error: str,
    invalid_response: str,
) -> str:
    """Request one bounded, auditable structural repair from the same model.

    The repaired document is still parsed and source-validated from scratch;
    this is not a permissive JSON fixer and never changes planner-owned ids.
    """

    error = str(validation_error or "unknown validation error")[:1000]
    response = str(invalid_response or "")[:48000]
    return (
        "你上一次返回的 Case / Path JSON 未通过 FORGE 的严格校验。"
        "只修复 JSON 语法或 schema 字段，不要解释，不要使用 Markdown，不要省略任何 Case 或 Path。"
        "所有面向用户展示的字段继续使用简体中文。"
        "必须返回完整 aceval.model-case-design/v1 对象；保留原 target Case ids、Requirement ids 和 source_refs，"
        "每个 Case 的真实任务 Prompt 必须保持互异。path step.match 仍只允许 event_type、tool_name、contains、fields。"
        "修复后的结果会从头重新经过 JSON、Case 覆盖、Requirement、Skill source refs、runtime 和 Path 校验。\n\n"
        "校验错误：%s\n\n待修复的模型输出：\n%s" % (error, response)
    )


def parse_model_case_design(
    content: str,
    *,
    plan: TestPlan,
    target_case_ids: Sequence[str],
) -> Mapping[str, Any]:
    value = _json_content(content)
    allowed_top = {"api_version", "cases", "paths", "generation_summary"}
    unknown = set(value).difference(allowed_top)
    if unknown:
        raise ModelCaseGenerationError("model case design contains unsupported fields: %s" % ", ".join(sorted(unknown)))
    if value.get("api_version") not in (None, MODEL_CASE_DESIGN_API_VERSION):
        raise ModelCaseGenerationError("unsupported model case design api_version")
    target = tuple(str(item) for item in target_case_ids)
    if not target or len(set(target)) != len(target):
        raise ModelCaseGenerationError("target case ids must be unique")
    req_by_id = {item.id: item for item in plan.requirements}
    planned_by_id = {item.id: item for item in plan.cases if item.id in set(target)}
    allowed_refs = {ref for item in plan.requirements for ref in item.source_refs}
    rows = value.get("cases")
    if not isinstance(rows, list) or any(not isinstance(item, Mapping) for item in rows):
        raise ModelCaseGenerationError("model cases must be an array of objects")
    if {str(item.get("id")) for item in rows} != set(target):
        raise ModelCaseGenerationError("model cases must cover exactly the planned generated Case ids")
    normalized = []
    seen_prompts = set()
    for item in rows:
        case_id = str(item.get("id") or "")
        title = _text(item.get("title"), "%s.title" % case_id, 240)
        prompt = _text(item.get("prompt"), "%s.prompt" % case_id)
        if prompt.casefold() in seen_prompts:
            raise ModelCaseGenerationError("model generated duplicate Case prompts")
        seen_prompts.add(prompt.casefold())
        family = _text(item.get("family"), "%s.family" % case_id, 240)
        kind = _text(item.get("kind"), "%s.kind" % case_id, 80)
        requirement_ids = _strings(item.get("requirement_ids"), "%s.requirement_ids" % case_id)
        if set(requirement_ids).difference(req_by_id):
            raise ModelCaseGenerationError("%s references an unknown requirement" % case_id)
        planned = planned_by_id.get(case_id)
        if planned is None or set(requirement_ids) != set(planned.requirement_ids):
            raise ModelCaseGenerationError("%s changed planner-owned requirement ids" % case_id)
        # ``family`` is presentation copy owned by the model (for example,
        # "代码审查 · 证据引用").  The deterministic planner's family remains
        # authoritative in ``planned.family`` and is preserved by the caller
        # as ``planner_family``; rejecting a readable model label here would
        # force the UI back to opaque ``cap.item-*`` identifiers.
        planned_dimensions = {req_by_id[req_id].dimension for req_id in planned.requirement_ids}
        # Safety-critical dimension and runtime are planner-owned.  Models may
        # use a human label (e.g. "artifact presence") or over-specify a
        # capability; normalize those claims to the frozen plan while keeping
        # the original values auditable in the returned design.
        model_kind = kind
        normalized_kind = next(iter(planned_dimensions))
        source_refs = _strings(item.get("source_refs"), "%s.source_refs" % case_id)
        requirement_refs = {ref for req_id in requirement_ids for ref in req_by_id[req_id].source_refs}
        if set(source_refs).difference(allowed_refs) or set(source_refs).difference(requirement_refs):
            raise ModelCaseGenerationError("%s references an unknown Skill source" % case_id)
        expected = _strings(item.get("expected_observables"), "%s.expected_observables" % case_id)
        oracle = _text(item.get("oracle_strategy"), "%s.oracle_strategy" % case_id, 240)
        runtime = _strings(item.get("required_runtime_capabilities", ()), "%s.required_runtime_capabilities" % case_id, required=False)
        unknown_runtime = set(runtime).difference(plan.runtime_capabilities)
        normalized.append({
            "id": case_id, "title": title, "prompt": prompt, "family": family,
            "kind": normalized_kind, "model_kind": model_kind,
            "requirement_ids": list(planned.requirement_ids),
            "source_refs": list(source_refs), "generation_reason": _text(item.get("generation_reason"), "%s.generation_reason" % case_id, 1000),
            "expected_observables": list(expected), "oracle_strategy": oracle,
            "required_runtime_capabilities": list(planned.required_runtime_capabilities),
            "model_required_runtime_capabilities": list(runtime),
            "undeclared_runtime_capabilities": sorted(unknown_runtime),
        })
    paths_raw = value.get("paths")
    if not isinstance(paths_raw, list):
        raise ModelCaseGenerationError("model paths must be an array")
    by_case = {}
    for path in paths_raw:
        if not isinstance(path, Mapping):
            raise ModelCaseGenerationError("model path must be an object")
        case_id = str(path.get("case_id") or "")
        if case_id not in target or case_id in by_case:
            raise ModelCaseGenerationError("model paths must cover each Case exactly once")
        steps = path.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ModelCaseGenerationError("path %s must contain steps" % case_id)
        ids = set()
        normalized_steps = []
        for step in steps:
            if not isinstance(step, Mapping):
                raise ModelCaseGenerationError("path step must be an object")
            step_id = _text(step.get("id"), "path step id", 120)
            if step_id in ids:
                raise ModelCaseGenerationError("path %s has duplicate step ids" % case_id)
            ids.add(step_id)
            kind = _text(step.get("kind"), "path step kind", 40)
            if kind not in ("required", "recommended", "forbidden", "alternative"):
                raise ModelCaseGenerationError("path step kind is invalid")
            match = step.get("match", {})
            if not isinstance(match, Mapping):
                raise ModelCaseGenerationError("path step match must be an object")
            unknown_match = set(match).difference({"event_type", "tool_name", "contains", "fields"})
            if unknown_match or not match:
                raise ModelCaseGenerationError("path step match contains unsupported or empty fields")
            if "fields" in match and not isinstance(match["fields"], Mapping):
                raise ModelCaseGenerationError("path step match.fields must be an object")
            after = _strings(step.get("after", ()), "path step after", required=False)
            if set(after).difference(ids):
                raise ModelCaseGenerationError("path step references a future or unknown predecessor")
            if kind == "alternative" and not isinstance(step.get("alternative_group"), str):
                raise ModelCaseGenerationError("alternative path steps require alternative_group")
            normalized_step = {"id": step_id, "label": _text(step.get("label"), "path step label", 500), "kind": kind, "match": dict(match), "after": list(after)}
            if kind == "alternative":
                normalized_step["alternative_group"] = str(step["alternative_group"])
            normalized_steps.append(normalized_step)
        by_case[case_id] = {"case_id": case_id, "purpose": _text(path.get("purpose") or "Case-specific execution path", "path purpose", 500), "steps": normalized_steps}
    if set(by_case) != set(target):
        raise ModelCaseGenerationError("model paths must cover exactly the generated Cases")
    summary = value.get("generation_summary", {})
    if summary is not None and not isinstance(summary, Mapping):
        raise ModelCaseGenerationError("generation_summary must be an object")
    return {"cases": normalized, "paths": list(by_case.values()), "generation_summary": dict(summary or {})}


def model_profile(model: ModelClient) -> Mapping[str, Any]:
    value = getattr(model, "profile", None)
    if callable(value):
        value = value()
    return dict(value) if isinstance(value, Mapping) else {"profile": str(value or "unknown")}


__all__ = [
    "MODEL_CASE_DESIGN_API_VERSION", "ModelCaseGenerationError",
    "build_case_generation_prompt", "build_case_generation_repair_prompt",
    "parse_model_case_design", "model_profile",
]
