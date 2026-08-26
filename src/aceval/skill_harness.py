"""Capability planning and greenfield construction for the Skill Harness."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .agent_runtime import ModelClient
from .kernel_contracts import KernelInput
from .optimizer import CandidateRejected, MaterializedCandidateSnapshot
from .skill_tree_optimizer import (
    SkillTreePatchPolicy,
    is_editable_skill_path,
    materialize_skill_tree_candidate,
    scan_skill_tree,
)


CAPABILITY_BLUEPRINT_API_VERSION = "aceval.skill-harness-blueprint/v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SkillHarnessError(RuntimeError):
    pass


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise SkillHarnessError("planned file path must be a safe POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in ("", ".", "..") for part in path.parts):
        raise SkillHarnessError("planned file path must be a safe POSIX relative path")
    return value


def _array(value: Any, label: str) -> list:
    if not isinstance(value, list):
        raise SkillHarnessError("%s must be an array" % label)
    return value


def _text(value: Any, label: str, maximum: int = 16_000) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum or "\x00" in value:
        raise SkillHarnessError("%s must be a trimmed non-empty string" % label)
    return value


def _strict_clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise SkillHarnessError("Harness blueprint must be strict JSON") from exc


def validate_blueprint(
    value: Any,
    *,
    operation: str,
    existing_paths: Sequence[str],
    policy: SkillTreePatchPolicy,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SkillHarnessError("capability architect must return an object")
    proposals = _array(value.get("proposals"), "proposals")
    if not proposals:
        raise SkillHarnessError("capability blueprint requires at least one proposal")
    if operation == "discover" and len(proposals) < 2:
        raise SkillHarnessError("discover mode requires at least two capability proposals")
    if len(proposals) > 8:
        raise SkillHarnessError("capability blueprint supports at most eight proposals")
    known = set(existing_paths)
    ids = set()
    global_case_ids = set()
    normalized = []
    for index, raw in enumerate(proposals):
        if not isinstance(raw, Mapping):
            raise SkillHarnessError("proposal must be an object")
        proposal_id = _text(raw.get("id"), "proposal.id", 128)
        if not _ID.fullmatch(proposal_id) or proposal_id in ids:
            raise SkillHarnessError("proposal ids must be unique safe identifiers")
        ids.add(proposal_id)
        planned = []
        for file_value in _array(raw.get("planned_files"), "proposal.planned_files"):
            if not isinstance(file_value, Mapping):
                raise SkillHarnessError("planned file must be an object")
            path = _path(file_value.get("path"))
            action = file_value.get("action")
            if action not in ("modify", "create"):
                raise SkillHarnessError("planned file action must be modify or create")
            if operation == "create" and action != "create":
                raise SkillHarnessError("greenfield create blueprint may only create files")
            if not is_editable_skill_path(path, policy):
                raise SkillHarnessError("planned file is outside editable Skill resources: %s" % path)
            if action == "modify" and path not in known:
                raise SkillHarnessError("planned modify path does not exist: %s" % path)
            if action == "create" and path in known:
                raise SkillHarnessError("planned create path already exists: %s" % path)
            planned.append({"path": path, "action": action, "reason": _text(file_value.get("reason"), "planned_file.reason", 4096)})
        if not planned:
            raise SkillHarnessError("proposal requires at least one planned file")
        cases = []
        case_ids = set()
        for case_value in _array(raw.get("cases"), "proposal.cases"):
            if not isinstance(case_value, Mapping):
                raise SkillHarnessError("blueprint Case must be an object")
            case_id = _text(case_value.get("id"), "proposal.case.id", 128)
            if not _ID.fullmatch(case_id) or case_id in case_ids or case_id in global_case_ids:
                raise SkillHarnessError("blueprint Case ids must be globally unique safe identifiers")
            case_ids.add(case_id)
            global_case_ids.add(case_id)
            kind = case_value.get("kind")
            if kind not in ("capability", "boundary", "negative_trigger", "regression"):
                raise SkillHarnessError("blueprint Case kind is invalid")
            case = {
                "id": case_id,
                "prompt": _text(case_value.get("prompt"), "proposal.case.prompt", 32_000),
                "metadata": {"source": "capability_blueprint", "capability_id": proposal_id, "harness_case_kind": kind},
            }
            if "expected_output" in case_value:
                case["expected_output"] = _strict_clone(case_value["expected_output"])
            cases.append(case)
        if not cases:
            raise SkillHarnessError("proposal requires at least one evaluation Case")
        case_kinds = {str(item["metadata"]["harness_case_kind"]) for item in cases}
        required_case_kinds = {"capability", "boundary", "negative_trigger"}
        if operation != "create":
            required_case_kinds.add("regression")
        if not required_case_kinds.issubset(case_kinds):
            raise SkillHarnessError(
                "proposal cases must cover: %s" % ", ".join(sorted(required_case_kinds))
            )
        required_nonempty_arrays = {
            "triggers": "proposal.triggers",
            "inputs": "proposal.inputs",
            "outputs": "proposal.outputs",
            "workflow": "proposal.workflow",
            "acceptance_criteria": "proposal.acceptance_criteria",
            "evidence": "proposal.evidence",
            "risks": "proposal.risks",
        }
        for field, label in required_nonempty_arrays.items():
            if not _array(raw.get(field), label):
                raise SkillHarnessError("%s must not be empty" % label)
        normalized.append({
            "id": proposal_id,
            "title": _text(raw.get("title"), "proposal.title", 512),
            "problem": _text(raw.get("problem"), "proposal.problem"),
            "user_value": _text(raw.get("user_value"), "proposal.user_value"),
            "triggers": [_text(item, "proposal.triggers[]", 1024) for item in _array(raw.get("triggers"), "proposal.triggers")],
            "inputs": [_text(item, "proposal.inputs[]", 2048) for item in _array(raw.get("inputs"), "proposal.inputs")],
            "outputs": [_text(item, "proposal.outputs[]", 2048) for item in _array(raw.get("outputs"), "proposal.outputs")],
            "workflow": [_text(item, "proposal.workflow[]", 4096) for item in _array(raw.get("workflow"), "proposal.workflow")],
            "dependencies": [_text(item, "proposal.dependencies[]", 2048) for item in _array(raw.get("dependencies"), "proposal.dependencies")],
            "acceptance_criteria": [_text(item, "proposal.acceptance_criteria[]", 4096) for item in _array(raw.get("acceptance_criteria"), "proposal.acceptance_criteria")],
            "evidence": [_text(item, "proposal.evidence[]", 4096) for item in _array(raw.get("evidence"), "proposal.evidence")],
            "risks": [_text(item, "proposal.risks[]", 4096) for item in _array(raw.get("risks"), "proposal.risks")],
            "planned_files": planned,
            "cases": cases,
        })
    recommended = _array(value.get("recommended_proposal_ids"), "recommended_proposal_ids")
    if any(not isinstance(item, str) or item not in ids for item in recommended) or len(recommended) != len(set(recommended)):
        raise SkillHarnessError("recommended proposal ids must reference proposals")
    if operation != "discover" and not recommended:
        recommended = [normalized[0]["id"]]
    selected = list(recommended) if operation != "discover" else []
    if operation == "create":
        create_paths = {
            item["path"]
            for proposal in normalized
            if proposal["id"] in selected
            for item in proposal["planned_files"]
            if item["action"] == "create"
        }
        if "SKILL.md" not in create_paths and "src/SKILL.md" not in create_paths:
            raise SkillHarnessError("create blueprint must create SKILL.md or src/SKILL.md")
    return {
        "api_version": CAPABILITY_BLUEPRINT_API_VERSION,
        "operation": operation,
        "goal": _text(value.get("goal"), "blueprint.goal"),
        "summary": _text(value.get("summary"), "blueprint.summary"),
        "proposals": normalized,
        "recommended_proposal_ids": list(recommended),
        "selected_proposal_ids": selected,
        "regression_required": operation != "create",
        "usage": _strict_clone(value.get("usage", {})),
    }


class CapabilityArchitect:
    """Generate a reviewable capability blueprint before Skill construction."""

    def __init__(self, model: ModelClient) -> None:
        self.model = model

    def plan(
        self,
        *,
        operation: str,
        user_input: KernelInput,
        existing_paths: Sequence[str],
        current_skill: Optional[str],
        policy: SkillTreePatchPolicy,
    ) -> Mapping[str, Any]:
        request = {
            "operation": operation,
            "skill_name": user_input.skill_name,
            "goal": user_input.effective_goal,
            "requested_capabilities": list(user_input.capabilities),
            "standards": list(user_input.effective_standards),
            "non_goals": list(user_input.non_goals),
            "user_cases": [case.to_dict() for case in user_input.cases],
            "existing_resource_inventory": list(existing_paths),
            "current_skill_excerpt": current_skill[:24_000] if current_skill else None,
            "rules": {
                "progressive_disclosure": "Keep SKILL.md focused; put detailed domain knowledge in references and deterministic repeated work in scripts.",
                "independent_evaluation": "Cases and acceptance criteria are frozen outside the candidate repository before building.",
                "scope": "Use exact existing paths for modify and safe new bundled-resource paths for create.",
                "discovery": "Discovery proposals need evidence, value, risks, and must not silently become implementation.",
                "file_actions": "Create mode may only use create; extend/discover may modify existing inventory paths and create new bundled resources.",
                "case_coverage": "Every proposal needs capability, boundary, and negative_trigger cases; existing Skills also need a regression case.",
            },
            "output_contract": {
                "goal": "resolved target",
                "summary": "short plan summary",
                "proposals": [{
                    "id": "safe-id", "title": "", "problem": "", "user_value": "",
                    "triggers": [], "inputs": [], "outputs": [], "workflow": [], "dependencies": [],
                    "acceptance_criteria": [], "evidence": [], "risks": [],
                    "planned_files": [{"path": "SKILL.md", "action": "create" if operation == "create" else "modify|create", "reason": ""}],
                    "cases": [{"id": "safe-id", "kind": "capability|boundary|negative_trigger|regression", "prompt": "", "expected_output": "optional"}],
                }],
                "recommended_proposal_ids": [],
            },
        }
        reply = self.model.complete(
            [
                {"role": "system", "content": "You are the Skill Harness capability architect. Return strict JSON only. Design general reusable capabilities and reviewable independent acceptance evidence; do not implement files."},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            (),
        )
        if reply.tool_calls:
            raise SkillHarnessError("capability architect may not call tools")
        try:
            payload = json.loads(reply.content)
        except json.JSONDecodeError as exc:
            raise SkillHarnessError("capability architect returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SkillHarnessError("capability architect must return an object")
        payload["usage"] = dict(reply.usage or {})
        return validate_blueprint(payload, operation=operation, existing_paths=existing_paths, policy=policy)


def select_blueprint(value: Mapping[str, Any], proposal_ids: Sequence[str]) -> Mapping[str, Any]:
    proposals = {str(item.get("id")): item for item in value.get("proposals", ()) if isinstance(item, Mapping)}
    selected = tuple(dict.fromkeys(str(item) for item in proposal_ids))
    if not selected or any(item not in proposals for item in selected):
        raise SkillHarnessError("selected capability ids must reference at least one proposal")
    result = _strict_clone(value)
    result["selected_proposal_ids"] = list(selected)
    result["operation"] = "extend" if value.get("operation") == "discover" else value.get("operation")
    return result


def selected_blueprint_cases(value: Mapping[str, Any]) -> Tuple[Mapping[str, Any], ...]:
    selected = set(str(item) for item in value.get("selected_proposal_ids", ()))
    cases = []
    seen = set()
    for proposal in value.get("proposals", ()):
        if not isinstance(proposal, Mapping) or str(proposal.get("id")) not in selected:
            continue
        for case in proposal.get("cases", ()):
            if isinstance(case, Mapping) and str(case.get("id")) not in seen:
                cases.append(_strict_clone(case))
                seen.add(str(case.get("id")))
    return tuple(cases)


def selected_file_plan(value: Mapping[str, Any]) -> Tuple[Mapping[str, str], ...]:
    selected = set(str(item) for item in value.get("selected_proposal_ids", ()))
    files: Dict[str, Mapping[str, str]] = {}
    for proposal in value.get("proposals", ()):
        if not isinstance(proposal, Mapping) or str(proposal.get("id")) not in selected:
            continue
        for item in proposal.get("planned_files", ()):
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("path"))
            prior = files.get(path)
            if prior and prior.get("action") != item.get("action"):
                raise SkillHarnessError("selected proposals conflict on file action: %s" % path)
            files[path] = {"path": path, "action": str(item.get("action")), "reason": str(item.get("reason"))}
    return tuple(files[path] for path in sorted(files))


def _validate_new_skill_entry(path: str, content: str, skill_name: str) -> None:
    if path not in ("SKILL.md", "src/SKILL.md"):
        return
    if not content.startswith("---\n") or "\n---\n" not in content[4:]:
        raise CandidateRejected("new SKILL.md must contain YAML frontmatter")
    frontmatter = content.split("\n---\n", 1)[0][4:]
    names = re.findall(r"(?m)^name:\s*['\"]?([^'\"\n]+)", frontmatter)
    descriptions = re.findall(r"(?m)^description:\s*(?:\||>)?\s*(.*)$", frontmatter)
    if not names or names[0].strip() != skill_name:
        raise CandidateRejected("new SKILL.md frontmatter name must match skill_name")
    if not descriptions:
        raise CandidateRejected("new SKILL.md frontmatter requires description")


class SkillProjectBuilder:
    """Build the first immutable candidate for a greenfield Skill repository."""

    def __init__(self, model: ModelClient) -> None:
        self.model = model

    def build(
        self,
        *,
        subject: Path,
        blueprint: Mapping[str, Any],
        skill_name: str,
        output_root: Path,
        policy: SkillTreePatchPolicy,
    ) -> MaterializedCandidateSnapshot:
        file_plan = selected_file_plan(blueprint)
        create_paths = tuple(item["path"] for item in file_plan if item["action"] == "create")
        if not create_paths:
            raise CandidateRejected("greenfield Skill blueprint has no files to create")
        build_blueprint = _strict_clone(blueprint)
        selected = set(str(item) for item in build_blueprint.get("selected_proposal_ids", ()))
        build_blueprint["proposals"] = [
            proposal
            for proposal in build_blueprint.get("proposals", ())
            if isinstance(proposal, dict) and str(proposal.get("id")) in selected
        ]
        # Acceptance criteria shape the implementation, but evaluation prompts
        # and expected outputs remain frozen outside the candidate context.
        for proposal in build_blueprint.get("proposals", ()):
            if isinstance(proposal, dict):
                proposal.pop("cases", None)
        request = {
            "skill_name": skill_name,
            "blueprint": build_blueprint,
            "approved_create_paths": list(create_paths),
            "instructions": {
                "skill_md": "Use valid name/description frontmatter, imperative workflow, explicit output contract, and progressive disclosure.",
                "resources": "Put deterministic repeated work in scripts and detailed optional knowledge in references.",
                "scope": "Create every necessary approved file and no other files.",
            },
            "output_contract": {"files": [{"path": "approved/path", "content": "complete UTF-8 content", "reason": ""}], "rationale": ""},
        }
        reply = self.model.complete(
            [
                {"role": "system", "content": "You are the Skill Harness greenfield builder. Return strict JSON only. Build a concise general Skill from the frozen blueprint; do not include evaluation Case literals or modify the blueprint."},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            (),
        )
        if reply.tool_calls:
            raise CandidateRejected("greenfield builder may not call tools", reply.usage)
        try:
            payload = json.loads(reply.content)
        except json.JSONDecodeError as exc:
            raise CandidateRejected("greenfield builder returned invalid JSON", reply.usage) from exc
        files = payload.get("files") if isinstance(payload, Mapping) else None
        if not isinstance(files, list) or not files or any(not isinstance(item, Mapping) for item in files):
            raise CandidateRejected("greenfield builder files must be a non-empty array", reply.usage)
        returned = tuple(str(item.get("path")) for item in files)
        if len(set(returned)) != len(returned) or set(returned) != set(create_paths):
            raise CandidateRejected("greenfield builder must create exactly the approved files", reply.usage)
        changes = []
        for item in files:
            path = _path(item.get("path"))
            content = item.get("content")
            if not isinstance(content, str):
                raise CandidateRejected("greenfield file content must be UTF-8 text", reply.usage)
            _validate_new_skill_entry(path, content, skill_name)
            changes.append({"path": path, "operation": "create_file", "content": content, "reason": str(item.get("reason", ""))})
        parent_files = scan_skill_tree(Path(subject), policy, require_entrypoint=False)
        return materialize_skill_tree_candidate(
            Path(subject),
            parent_files,
            changes,
            str(payload.get("rationale", "")),
            Path(output_root),
            create_paths,
            policy,
            create_paths=create_paths,
            usage=reply.usage,
        )


__all__ = [
    "CAPABILITY_BLUEPRINT_API_VERSION", "CapabilityArchitect", "SkillHarnessError",
    "SkillProjectBuilder", "select_blueprint", "selected_blueprint_cases",
    "selected_file_plan", "validate_blueprint",
]
