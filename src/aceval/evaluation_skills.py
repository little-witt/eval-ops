"""Versioned node Skills for the evaluation and optimization loop.

Each node has a small, explicit contract.  The contracts are intentionally
data-first so the kernel can persist their versions and input hashes beside
model receipts without coupling the UI to a particular provider.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


NODE_SKILLS_API_VERSION = "aceval.node-skills/v1"

NODE_SKILLS = {
    "eval-case-planner": {
        "version": "1.0.0",
        "purpose": "从被测 Skill immutable snapshot 生成稳定、互异、可执行的 Case 与执行路径",
        "output_schema": "aceval.model-case-design/v1",
        "rules": (
            "相同 skill_hash、评测目标、标准、种子 Case 和 planner_version 必须复用相同设计；"
            "每个 Case 必须绑定 Skill source_refs、明确输入/输出/观察点，并与其它 Case 具有不同测试意图；"
            "模型输出不通过 schema 或来源校验时必须失败，禁止静默降级为看似有效的模板 Case。"
        ),
    },
    "single-case-evaluator": {
        "version": "1.0.0",
        "purpose": "基于单个 Case 的完整 Trace 和输出形成稳定、可审计的结论",
        "output_schema": "aceval.semantic-verdict/v1",
        "rules": (
            "每次只评一个 Case；结论必须区分满足、未满足和证据不足；"
            "失败结论必须引用具体输出或 Trace 事实，并区分 Skill、Agent、环境和评测标准归因；"
            "禁止复制日志、拼接执行过程或用缺失证据推断 Skill 失败。"
        ),
    },
    "skill-diagnostician": {
        "version": "1.0.0",
        "purpose": "汇总独立 Case 结论，归纳 Skill 的实质问题和最小优化方向",
        "output_schema": "aceval.attribution-report/v1 + aceval.optimization-plan/v1",
        "rules": (
            "只有多个失败事实指向同一 Skill 规则时才聚类为共性问题；"
            "每个问题只保留一句问题、一条关键证据、Skill 原文位置、影响 Case 和可证伪根因；"
            "必须明确 Skill 缺陷、模型偏差、环境问题和评测问题，不得用日志路径或 Case 列表充当结论。"
        ),
    },
    "skill-optimizer": {
        "version": "1.0.0",
        "purpose": "根据已批准的问题生成受限、可验证、可回滚的 Skill 候选修改",
        "output_schema": "aceval.optimizer-candidate/v1",
        "rules": (
            "只能修改批准的文件和范围；每条修改必须关联已确认的失败事实和验证 Case；"
            "候选必须通过范围、结构、引用和本地校验；失败时停止，不得发布半成品；"
            "发布后必须复用原 Case、Fixture 和评测契约进行 Challenger 回归。"
        ),
    },
}


def canonical_hash(value: Any) -> str:
    """Return a stable hash for node inputs and immutable evaluation context."""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def node_skill(skill_id: str) -> Mapping[str, Any]:
    if skill_id not in NODE_SKILLS:
        raise KeyError("unknown evaluation node Skill: %s" % skill_id)
    value = dict(NODE_SKILLS[skill_id])
    value.update({"id": skill_id, "api_version": NODE_SKILLS_API_VERSION})
    return value


def node_skill_context(skill_id: str) -> str:
    value = node_skill(skill_id)
    return (
        "节点 Skill 契约（%s v%s）：%s。输出契约：%s。执行规则：%s"
        % (value["id"], value["version"], value["purpose"], value["output_schema"], value["rules"])
    )


def node_skill_manifest() -> Mapping[str, Any]:
    return {"api_version": NODE_SKILLS_API_VERSION, "skills": {key: node_skill(key) for key in NODE_SKILLS}}

