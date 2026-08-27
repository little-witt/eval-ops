"""Application Service for low-token, user-confirmed Skill iteration.

This module is intentionally UI-agnostic. A future desktop application can
drive the complete workflow through create/compile/dispatch/collect/analyze/
confirm/optimize/advance without understanding EvalPack or CATX internals.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple, Union
import uuid

from .agent_runtime import CommandModelClient, ModelClient
from .catx import CatxAgentClient, CatxAgentProfile, CatxRepositoryResourceProfile
from .catx_bindings import CatxExecutionBinding, CatxPayloadBindingAdapter, CATX_EXECUTION_BINDING_API_VERSION
from .contracts import as_primitive
from .evaluation_compiler import classify_evaluation, compile_evaluation, default_output_path
from .evidence_analysis import CrossCaseAnalyzer
from .iteration_brain import IterationBrain
from .execution_path import EXECUTION_PATH_SPEC_API_VERSION
from .git_publisher import GitSkillPublisher
from .kernel_contracts import (
    EVALUATION_DESIGN_API_VERSION,
    KernelConfig,
    KernelInput,
    UserCase,
    contract_hash,
)
from .kernel_v2 import compare_candidates, convergence_state
from .optimizer import FailureEvidence
from .pack import EvalPackLoader
from .pack_lifecycle import pack_calibration_status
from .planning_workflow import REFERENCE_RUNTIME_CAPABILITIES, create_planning_artifacts
from .model_case_generation import (
    MODEL_CASE_DESIGN_API_VERSION,
    ModelCaseGenerationError,
    build_case_generation_prompt,
    build_case_generation_repair_prompt,
    model_profile,
    parse_model_case_design,
)
from .registry import build_builtin_registry
from .remote_batch import RemoteBatchCoordinator, RemoteBatchError, SessionGateway
from .repository_checkout import GitCheckoutManager
from .skill_tree_optimizer import SkillTreeOptimizer, SkillTreePatchPolicy, editable_skill_inventory, is_editable_skill_path, scan_skill_tree
from .skill_harness import (
    CapabilityArchitect,
    SkillHarnessError,
    SkillProjectBuilder,
    select_blueprint,
    selected_blueprint_cases,
    selected_file_plan,
)
from .task_center import TaskStore
from .trial_environment import (
    TrialEnvironmentContractError,
    build_trial_environment_contract,
    verify_contract_hash,
)


KERNEL_STATE_API_VERSION = "aceval.iteration-kernel-state/v1"


class IterationKernelError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))
    path.chmod(0o600)


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IterationKernelError("cannot read %s" % label) from exc
    if not isinstance(value, Mapping):
        raise IterationKernelError("%s must be an object" % label)
    return value


def _skill_file(root_value: str) -> Path:
    root = Path(root_value).expanduser().resolve()
    if root.is_file() and root.name == "SKILL.md":
        return root
    for candidate in (root / "SKILL.md", root / "src" / "SKILL.md"):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise IterationKernelError("local Skill checkout must contain SKILL.md or src/SKILL.md")


def _skill_root(root_value: str) -> Path:
    root = Path(root_value).expanduser().resolve()
    return root.parent if root.is_file() else root


def _optional_skill_file(root_value: str) -> Optional[Path]:
    try:
        return _skill_file(root_value)
    except IterationKernelError:
        return None


def _optimization_policy(config: KernelConfig) -> SkillTreePatchPolicy:
    policy = config.policy
    return SkillTreePatchPolicy(
        editable_patterns=tuple(policy.optimization_editable_patterns),
        max_changed_files=policy.max_optimization_changed_files,
        max_added_lines=policy.max_optimization_added_lines,
        max_changed_bytes=policy.max_optimization_changed_bytes,
        max_context_chars=policy.max_optimization_context_chars,
        validation_commands=tuple(policy.optimization_validation_commands),
    )


def _case_document(value: UserCase) -> Mapping[str, Any]:
    result = dict(value.to_dict())
    metadata = dict(result.get("metadata", {}))
    # Stable provenance/reuse identity is intentionally local and immutable;
    # broad cross-task mining is deferred, but completed cases can already be
    # matched safely by revision and environment applicability.
    fingerprint = contract_hash({"id": result["id"], "prompt": result["prompt"], "expected_output": result.get("expected_output"), "metadata": metadata})
    metadata.setdefault("case_revision", fingerprint)
    metadata.setdefault("reuse_key", contract_hash({"prompt": result["prompt"], "expected_output": result.get("expected_output")}))
    metadata.setdefault("provenance", {"source": metadata.get("source", "user"), "history": []})
    result["metadata"] = metadata
    return result


def _normalize_case_identity(
    case: Mapping[str, Any],
    *,
    goal: str,
    standards: Sequence[str],
    capability_contract: str,
    environment_applicability: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Give every case source one stable, reusable identity."""
    result = dict(case)
    metadata = dict(result.get("metadata", {})) if isinstance(result.get("metadata"), Mapping) else {}
    source = metadata.get("source") or "automatic"
    if source not in ("user", "default", "automatic", "blueprint", "custom_evalpack", "custom_pack"):
        source = str(source)
    provenance = metadata.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {"source": source}
    else:
        provenance = dict(provenance); provenance.setdefault("source", source)
    content = {"id": result.get("id"), "prompt": result.get("prompt"), "expected_output": result.get("expected_output"), "metadata": {k: v for k, v in metadata.items() if k not in ("case_revision", "content_hash", "reuse_key", "provenance")}}
    content_hash = contract_hash(content)
    case_revision = contract_hash({"content_hash": content_hash, "provenance": provenance})
    applicability = metadata.get("environment_applicability")
    if not isinstance(applicability, Mapping):
        applicability = dict(environment_applicability)
    else:
        applicability = dict(applicability)
    reuse_key = contract_hash({"goal": goal, "standards": list(standards), "capability_contract": capability_contract, "environment_applicability": applicability, "content_hash": content_hash})
    result.update({"case_revision": case_revision, "content_hash": content_hash, "provenance": provenance, "environment_applicability": applicability, "reuse_key": reuse_key})
    metadata.update({"case_revision": case_revision, "content_hash": content_hash, "provenance": provenance, "environment_applicability": applicability, "reuse_key": reuse_key})
    result["metadata"] = metadata
    return result


def _default_seed(user_input: KernelInput) -> Tuple[Mapping[str, Any], ...]:
    if user_input.cases:
        return tuple(_case_document(item) for item in user_input.cases)
    return (
        {
            "id": "auto-primary-goal",
            "prompt": "请执行该 Skill 的主要能力并达成以下目标：%s" % user_input.effective_goal,
            "metadata": {"source": "automatic", "input_mode": user_input.mode},
        },
    )


def _custom_evalpack_seed(path: str) -> Tuple[Any, Tuple[Mapping[str, Any], ...]]:
    pack = EvalPackLoader(build_builtin_registry()).load(path)
    scenarios = [item for item in pack.scenarios if item.split == "dev"]
    if not scenarios:
        scenarios = [item for item in pack.scenarios if item.split != "holdout"]
    cases = []
    for item in scenarios:
        metadata = dict(as_primitive(item.scenario.metadata))
        metadata.update({"source": "custom_evalpack", "eval_split": item.split, "fixture_refs": list(item.fixtures)})
        case: dict[str, Any] = {"id": item.id, "prompt": item.prompt, "metadata": metadata}
        oracle = as_primitive(item.oracle.data) if item.oracle is not None else None
        if isinstance(oracle, Mapping) and "expected_output" in oracle:
            case["expected_output"] = oracle["expected_output"]
        elif oracle is not None:
            case["expected_output"] = oracle
            metadata["expectation_mode"] = "semantic"
        cases.append(case)
    if not cases:
        raise IterationKernelError("custom EvalPack has no executable non-holdout scenarios")
    return pack, tuple(cases)


def _path_for_case(case: Mapping[str, Any], graph: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = case.get("metadata", {}) if isinstance(case.get("metadata"), Mapping) else {}
    test = metadata.get("aceval_test", {}) if isinstance(metadata.get("aceval_test"), Mapping) else {}
    kind = str(test.get("kind") or metadata.get("harness_case_kind") or "happy_path")
    title = str(test.get("title") or case.get("title") or case.get("id") or "Case")
    if metadata.get("harness_case_kind") == "negative_trigger":
        return {
            "api_version": EXECUTION_PATH_SPEC_API_VERSION,
            "trace_completeness_required": True,
            "steps": [{
                "id": "do-not-load-skill",
                "label": "近似请求不应触发候选 Skill",
                "kind": "forbidden",
                "match": {"event_type": "tool_call", "contains": "SKILL.md"},
                "after": [],
            }],
        }
    steps = [
        {
            "id": "load-skill",
            "label": "读取候选 Skill 指令 · %s" % title,
            "kind": "required",
            "match": {"event_type": "tool_call", "contains": "SKILL.md"},
            "after": [],
        }
    ]
    # Keep the path case-specific: only recommend tools declared by the
    # requirement/capability instead of repeating the entire Skill tool list.
    source_tool_names = set()
    capability_ids = test.get("capability_ids", ()) if isinstance(test.get("capability_ids"), list) else ()
    requirement_ids = test.get("requirement_ids", ()) if isinstance(test.get("requirement_ids"), list) else ()
    requirements = graph.get("capabilities", ()) if isinstance(graph.get("capabilities"), list) else ()
    for capability in requirements:
        if not isinstance(capability, Mapping):
            continue
        capability_tools = capability.get("tools", ())
        if isinstance(capability_tools, list) and str(capability.get("id", "")) in set(str(item) for item in capability_ids):
            source_tool_names.update(str(item) for item in capability_tools)
    if not source_tool_names:
        expected = test.get("expected_observables", ()) if isinstance(test.get("expected_observables"), list) else ()
        source_tool_names.update(
            str(tool.get("name")) for tool in (graph.get("tools", ()) if isinstance(graph.get("tools"), list) else ())
            if isinstance(tool, Mapping) and str(tool.get("name") or "") and any(str(tool.get("name")) in str(item) for item in expected)
        )
    for index, tool in enumerate(
        [tool for tool in (graph.get("tools", ()) if isinstance(graph.get("tools"), list) else ())
         if isinstance(tool, Mapping) and str(tool.get("name") or "") in source_tool_names]
    ):
        name = str(tool.get("name") or "").strip() if isinstance(tool, Mapping) else ""
        if not name:
            continue
        steps.append(
            {
                "id": "recommended-tool-%02d" % (index + 1),
                "label": "按需使用 Skill 声明的工具 %s" % name,
                "kind": "recommended",
                "match": {"tool_name": name},
                "after": ["load-skill"],
            }
        )
    expected = test.get("expected_observables", ()) if isinstance(test.get("expected_observables"), list) else ()
    model_validated = isinstance(metadata.get("generation_provenance"), Mapping) and metadata.get("generation_provenance", {}).get("strategy") == "cc-switch-model"
    if expected and model_validated:
        steps.append({
            "id": "assert-observable",
            "label": "核对该 Case 的预期可观察结果",
            "kind": "required",
            "match": {"contains": str(expected[0])[:500]},
            "after": ["load-skill"],
        })
    if kind in ("negative", "boundary", "recovery", "idempotency", "state_transition"):
        steps.append({
            "id": "exercise-boundary",
            "label": "执行 %s 边界并记录安全结果" % kind,
            "kind": "required",
            "match": {"contains": kind},
            "after": ["load-skill"],
        })
    steps.append(
        {
            "id": "no-skill-publish",
            "label": "评测会话不得发布或改写 Skill",
            "kind": "forbidden",
            "match": {"contains": "git push"},
            "after": [],
        }
    )
    return {
        "api_version": EXECUTION_PATH_SPEC_API_VERSION,
        "case_id": case.get("id"),
        "trace_completeness_required": True,
        "steps": steps,
    }


def _model_generated_cases(
    *,
    model: ModelClient,
    skill_text: str,
    graph: Mapping[str, Any],
    plan: Any,
    goal: str,
    standards: Sequence[str],
    seed_cases: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    artifact_root: Path,
) -> Tuple[list[Mapping[str, Any]], Mapping[str, Any], Optional[Mapping[str, Any]]]:
    """Ask the configured model for generated Case copy and paths.

    The deterministic planner remains authoritative for ids, requirements and
    runtime.  If the model is unavailable or returns an invalid design, return
    the deterministic cases with explicit fallback provenance.
    """
    generated = []
    for item in cases:
        metadata = item.get("metadata", {}) if isinstance(item, Mapping) else {}
        test = metadata.get("aceval_test", {}) if isinstance(metadata, Mapping) else {}
        if isinstance(test, Mapping) and test.get("origin") == "requirement_synthesis":
            generated.append(dict(item))
    target_ids = [str(item.get("id")) for item in generated]
    base_provenance = {
        "api_version": MODEL_CASE_DESIGN_API_VERSION,
        "strategy": "deterministic-fallback",
        "status": "not_requested" if not generated else "fallback",
        "generated_case_ids": target_ids,
        "model": model_profile(model),
        "model_id": str(getattr(model, "_model_id", None) or getattr(model, "model_id", None) or "unconfigured"),
        "artifact": str(artifact_root / "model-case-generation.json"),
    }
    if not generated:
        return list(cases), base_provenance, None
    prompt = build_case_generation_prompt(
        skill_text=skill_text,
        graph=graph,
        plan=plan,
        goal=goal,
        standards=standards,
        seed_cases=seed_cases,
        target_case_ids=target_ids,
    )
    prompt_hash = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    replies = []
    validation_errors = []

    def combined_usage() -> Mapping[str, Any]:
        totals = {}
        for candidate in replies:
            for key, value in dict(getattr(candidate, "usage", {}) or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0) + value
        return totals

    def write_receipt(status: str) -> None:
        _atomic_json(
            artifact_root / "agent-call-receipt-case-generation.json",
            {
                "api_version": "aceval.agent-call-receipt/v1",
                "stage": "case_generation",
                "provider": "cc-switch",
                "model_id": base_provenance.get("resolved_model_id") or base_provenance["model_id"],
                "configured_model_id": base_provenance["model_id"],
                "profile": base_provenance["model"],
                "input_hash": prompt_hash,
                "output_hashes": ["sha256:" + hashlib.sha256(item.content.encode("utf-8")).hexdigest() for item in replies],
                "usage": combined_usage(),
                "attempt_count": len(replies),
                "validation_errors": list(validation_errors),
                "status": status,
            },
        )

    try:
        messages = ({"role": "user", "content": prompt},)
        parsed = None
        for attempt in range(2):
            reply = model.complete(messages, ())
            replies.append(reply)
            if getattr(reply, "model_id", None):
                base_provenance["resolved_model_id"] = str(reply.model_id)
            try:
                parsed = parse_model_case_design(reply.content, plan=plan, target_case_ids=target_ids)
                break
            except ModelCaseGenerationError as exc:
                validation_errors.append(str(exc)[:1000])
                if attempt == 1:
                    raise
                messages = (
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": reply.content},
                    {"role": "user", "content": build_case_generation_repair_prompt(validation_error=str(exc), invalid_response=reply.content)},
                )
        if parsed is None:
            raise ModelCaseGenerationError("model Case generation did not produce a validated design")
        by_id = {str(item["id"]): item for item in parsed["cases"]}
        updated = []
        for item in cases:
            value = dict(item)
            if str(value.get("id")) in by_id:
                design = by_id[str(value.get("id"))]
                metadata = dict(value.get("metadata", {})) if isinstance(value.get("metadata"), Mapping) else {}
                test = dict(metadata.get("aceval_test", {})) if isinstance(metadata.get("aceval_test"), Mapping) else {}
                test.setdefault("planner_family", test.get("family"))
                test.update({key: design[key] for key in ("title", "family", "kind", "requirement_ids", "source_refs", "generation_reason", "expected_observables", "oracle_strategy", "required_runtime_capabilities")})
                for key in ("model_kind", "model_required_runtime_capabilities", "undeclared_runtime_capabilities"):
                    if design.get(key):
                        test[key] = design[key]
                test["family_id"] = design["family"]
                metadata["aceval_test"] = test
                metadata["generation_provenance"] = {"strategy": "cc-switch-model", "model_id": base_provenance["model_id"], "status": "validated"}
                value.update({"title": design["title"], "prompt": design["prompt"], "metadata": metadata})
            updated.append(value)
        path_by_case = {str(item["case_id"]): item for item in parsed["paths"]}
        base_provenance.update({
            "strategy": "cc-switch-model",
            "status": "validated",
            "configured_model_id": base_provenance["model_id"],
            "resolved_model_id": str(base_provenance.get("resolved_model_id") or base_provenance["model_id"]),
            "model_id": str(base_provenance.get("resolved_model_id") or base_provenance["model_id"]),
            "generation_summary": parsed.get("generation_summary", {}),
            "prompt_hash": prompt_hash,
            "usage": combined_usage(),
            "attempt_count": len(replies),
            "repair_applied": len(replies) > 1,
            "validation_errors": list(validation_errors),
            "artifact": str(artifact_root / "model-case-generation.json"),
            "receipt": str(artifact_root / "agent-call-receipt-case-generation.json"),
        })
        resolved_model_id = base_provenance["model_id"]
        for value in updated:
            if str(value.get("id")) not in by_id:
                continue
            metadata = dict(value.get("metadata", {})) if isinstance(value.get("metadata"), Mapping) else {}
            provenance = dict(metadata.get("generation_provenance", {})) if isinstance(metadata.get("generation_provenance"), Mapping) else {}
            provenance.update({"strategy": "cc-switch-model", "status": "validated", "model_id": resolved_model_id, "configured_model_id": base_provenance.get("configured_model_id")})
            metadata["generation_provenance"] = provenance
            value["metadata"] = metadata
        artifact = {
            "api_version": MODEL_CASE_DESIGN_API_VERSION,
            "provenance": base_provenance,
            "request": {"prompt_hash": base_provenance["prompt_hash"], "target_case_ids": target_ids},
            "response": parsed,
        }
        _atomic_json(artifact_root / "model-case-generation.json", artifact)
        write_receipt("validated")
        return updated, base_provenance, path_by_case
    except Exception as exc:
        base_provenance.update({
            "status": "fallback",
            "error": str(exc)[:500],
            "prompt_hash": prompt_hash,
            "usage": combined_usage(),
            "attempt_count": len(replies),
            "repair_applied": len(replies) > 1,
            "validation_errors": list(validation_errors),
            "receipt": str(artifact_root / "agent-call-receipt-case-generation.json"),
        })
        if base_provenance.get("resolved_model_id"):
            base_provenance["model_id"] = base_provenance["resolved_model_id"]
        _atomic_json(artifact_root / "model-case-generation.json", {
            "api_version": MODEL_CASE_DESIGN_API_VERSION,
            "provenance": base_provenance,
            "request": {"prompt_hash": prompt_hash, "target_case_ids": target_ids},
        })
        write_receipt("rejected")
        return list(cases), base_provenance, None


def _test_only_literals(cases: Sequence[Mapping[str, Any]]) -> Tuple[str, ...]:
    values = set()

    def collect(value: Any) -> None:
        if isinstance(value, str):
            if len(value) >= 8:
                values.add(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                collect(item)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                collect(item)

    for case in cases:
        collect(case.get("id"))
        collect(case.get("expected_output"))
        metadata = case.get("metadata", {})
        if isinstance(metadata, Mapping):
            for key in ("fixture_branch", "fixture_refs", "oracle_ref"):
                collect(metadata.get(key))
    return tuple(sorted(values))


def _safe_config_read_model(config: KernelConfig) -> Mapping[str, Any]:
    """Expose configuration topology without leaking accidental argv secrets."""

    value = dict(config.to_dict())
    local = dict(value.get("local_analysis", {}))
    command = list(config.local_analysis.model_command)
    local["model_command"] = [command[0]] + (["<%d arguments hidden>" % (len(command) - 1)] if len(command) > 1 else [])
    value["local_analysis"] = local
    return value


def _git_head(root_value: Optional[str]) -> Optional[str]:
    """Read an immutable repository revision without mutating the checkout."""

    if not root_value:
        return None
    root = Path(root_value).expanduser().resolve()
    if root.is_file():
        root = root.parent
    try:
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = completed.stdout.decode("ascii", errors="ignore").strip().lower()
    return revision if completed.returncode == 0 and len(revision) == 40 and all(item in "0123456789abcdef" for item in revision) else None


class IterationKernel:
    """Persisted state machine used by CLI today and a desktop app later."""

    def __init__(
        self,
        task_root: Union[str, Path] = ".aceval/tasks",
        *,
        gateway_factory: Optional[Callable[[KernelConfig], SessionGateway]] = None,
        model_factory: Optional[Callable[[KernelConfig], ModelClient]] = None,
        publisher: Optional[GitSkillPublisher] = None,
        checkout_manager: Optional[GitCheckoutManager] = None,
    ) -> None:
        self.store = TaskStore(task_root)
        self.gateway_factory = gateway_factory or self._default_gateway
        self.model_factory = model_factory or self._default_model
        self.publisher = publisher or GitSkillPublisher()
        self.checkout_manager = checkout_manager or GitCheckoutManager()

    def _kernel_dir(self, task_id: str) -> Path:
        return self.store.task_dir(task_id) / "kernel"

    def _state_path(self, task_id: str) -> Path:
        return self._kernel_dir(task_id) / "state.json"

    def _config(self, task_id: str) -> KernelConfig:
        return KernelConfig.from_mapping(_load_json(self._kernel_dir(task_id) / "config.json", "Kernel config"))

    def _input(self, task_id: str) -> KernelInput:
        return KernelInput.from_mapping(_load_json(self._kernel_dir(task_id) / "input.json", "Kernel input"))

    def _blueprint(self, task_id: str) -> Optional[Mapping[str, Any]]:
        state = self.state(task_id)
        path = state.get("blueprint")
        if not path:
            return None
        return _load_json(Path(str(path)), "capability blueprint")

    def state(self, task_id: str) -> Mapping[str, Any]:
        state = _load_json(self._state_path(task_id), "Kernel state")
        if state.get("api_version") != KERNEL_STATE_API_VERSION:
            raise IterationKernelError("unsupported Kernel state")
        return state

    def snapshot(self, task_id: str) -> Mapping[str, Any]:
        """Return the desktop-facing read model without exposing credentials."""

        state = self.state(task_id)
        result: dict[str, Any] = {
            "task": self.store.load(task_id),
            "state": state,
            "config": _safe_config_read_model(self._config(task_id)),
            "input": self._input(task_id).to_dict(),
            "events": list(self.store.events(task_id)),
            "iterations": [],
        }
        design = self._kernel_dir(task_id) / "active-design.json"
        if design.is_file():
            result["design"] = _load_json(design, "active evaluation design")
        blueprint = self._blueprint(task_id)
        if blueprint is not None:
            result["blueprint"] = blueprint
        decision = state.get("decision")
        if decision and Path(str(decision)).is_file():
            result["decision"] = _load_json(Path(str(decision)), "analysis decision")
        purpose = state.get("active_batch")
        if purpose:
            batch_path = self._persisted_batch_path(task_id, int(state.get("iteration", 0)), str(purpose))
            if batch_path.is_file():
                result["active_batch"] = _load_json(batch_path, "remote batch")
        iteration_root = self.store.task_dir(task_id) / "iterations"
        for directory in sorted(iteration_root.glob("iteration-*")):
            if not directory.is_dir():
                continue
            row: dict[str, Any] = {"name": directory.name, "batches": [], "session_logs": []}
            contract_path = directory / "environment-contract.json"
            if contract_path.is_file():
                row["environment_contract"] = _load_json(contract_path, "frozen trial environment contract")
            row["brain_stage"] = state.get("brain_stage") if directory.name == ("iteration-%03d" % int(state.get("iteration", 0))) else None
            row["approval_records"] = []
            for approval_path in sorted((directory / "approvals").glob("approval-record-*.json")):
                row["approval_records"].append(_load_json(approval_path, "approval record"))
            analysis_root = directory / "analysis"
            row["analysis_artifacts"] = {
                "agent_call_receipts": sorted(str(path) for path in analysis_root.glob("attempt-*/**/agent-call-receipt.json")),
                "evidence_query_receipts": sorted(str(path) for path in analysis_root.glob("evidence-query-receipt-*.json")),
            }
            planning_root = directory / "planning"
            if planning_root.is_dir():
                row["planning_artifacts"] = {
                    "model_case_generation": str(planning_root / "model-case-generation.json") if (planning_root / "model-case-generation.json").is_file() else None,
                    "case_generation_receipt": str(planning_root / "agent-call-receipt-case-generation.json") if (planning_root / "agent-call-receipt-case-generation.json").is_file() else None,
                }
            for batch_path in sorted((directory / "batches").glob("*.json")):
                batch = _load_json(batch_path, "remote batch")
                row["batches"].append(batch)
                row["session_logs"].extend(
                    {
                        "case_id": case.get("case_id"),
                        "case_title": case.get("case_title"),
                        "purpose": batch.get("purpose"),
                        "session_id": case.get("session_id"),
                        "status": case.get("status"),
                        "artifact": case.get("artifact"),
                        "repository_bindings": case.get("repository_bindings", []),
                        "binding_status": case.get("binding_status"),
                        "binding_evidence": case.get("binding_evidence"),
                        "message_status": case.get("message_status"),
                        "log_completeness": case.get("log_completeness"),
                    }
                    for case in batch.get("cases", ())
                    if isinstance(case, Mapping) and case.get("artifact")
                )
            decision_path = directory / "analysis-decision.json"
            if decision_path.is_file():
                row["decision"] = _load_json(decision_path, "analysis decision")
            candidate_path = directory / "candidates" / "candidate.json"
            if candidate_path.is_file():
                row["candidate"] = _load_json(candidate_path, "candidate")
            result["iterations"].append(row)
        return result

    def session_log(self, task_id: str, iteration: int, purpose: str, case_id: str) -> Mapping[str, Any]:
        """Resolve a full log through persisted batch state, never an arbitrary path."""

        batch = _load_json(self._persisted_batch_path(task_id, iteration, purpose), "remote batch")
        matches = [
            item for item in batch.get("cases", ())
            if isinstance(item, Mapping) and str(item.get("case_id")) == case_id
        ]
        if len(matches) != 1 or not matches[0].get("artifact"):
            raise IterationKernelError("case session log is not available")
        artifact = Path(str(matches[0]["artifact"])).resolve()
        task_directory = self.store.task_dir(task_id).resolve()
        if task_directory not in artifact.parents:
            raise IterationKernelError("case session log escaped its task directory")
        return _load_json(artifact, "case session log")

    def _persisted_batch_path(self, task_id: str, iteration: int, purpose: str) -> Path:
        """Resolve persisted batch state without constructing a remote gateway."""

        if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 0:
            raise IterationKernelError("iteration must be a non-negative integer")
        safe_purpose = "".join(item if item.isalnum() or item in "-_" else "-" for item in purpose).strip("-")
        if not safe_purpose or safe_purpose != purpose:
            raise IterationKernelError("batch purpose is invalid")
        return self.store.task_dir(task_id) / "iterations" / ("iteration-%03d" % iteration) / "batches" / (safe_purpose + ".json")

    def _environment_contract_path(self, task_id: str, iteration: int) -> Path:
        return self.store.task_dir(task_id) / "iterations" / ("iteration-%03d" % iteration) / "environment-contract.json"

    def _ensure_environment_contract(
        self,
        task_id: str,
        *,
        design: Mapping[str, Any],
        state: Mapping[str, Any],
        iteration: int,
    ) -> Mapping[str, Any]:
        """Create once, then compare, the immutable contract for this round."""
        config = self._config(task_id)
        path = self._environment_contract_path(task_id, iteration)
        current = build_trial_environment_contract(
            config,
            design,
            state,
            iteration=iteration,
            runtime_parameters={
                "max_parallel": config.remote_agent.max_parallel,
                "max_prompt_chars": config.policy.max_remote_prompt_chars,
                "timeout_seconds": config.remote_agent.max_wait_seconds,
            },
        )
        try:
            if path.is_symlink():
                raise IterationKernelError("frozen trial environment contract cannot be a symlink")
            if path.is_file():
                frozen = _load_json(path, "frozen trial environment contract")
                verify_contract_hash(frozen)
                if dict(frozen) != dict(current):
                    # created_at is provenance and must not participate in the
                    # comparison; all identity fields must remain unchanged.
                    frozen_identity = {k: v for k, v in frozen.items() if k not in ("created_at", "contract_hash")}
                    current_identity = {k: v for k, v in current.items() if k not in ("created_at", "contract_hash")}
                    if frozen_identity != current_identity:
                        raise IterationKernelError("trial environment drift detected; frozen contract no longer matches")
                contract = frozen
            else:
                contract = current
                _atomic_json(path, contract)
                self.store.append_event(
                    task_id,
                    "environment.contract_frozen",
                    {"contract": str(path), "environment_contract_hash": contract["contract_hash"]},
                    iteration=iteration,
                )
        except TrialEnvironmentContractError as exc:
            raise IterationKernelError(str(exc)) from exc
        explicit = config.trial_executor.environment_contract_hash if config.trial_executor else None
        if explicit and explicit != contract.get("contract_hash"):
            raise IterationKernelError("trial_executor.environment_contract_hash does not match frozen contract")
        if state.get("environment_contract_hash") not in (None, contract.get("contract_hash")):
            raise IterationKernelError("state environment contract does not match frozen contract")
        if state.get("environment_contract") != str(path) or state.get("environment_contract_hash") != contract.get("contract_hash"):
            self._transition(task_id, str(state.get("phase")), environment_contract=str(path), environment_contract_hash=contract.get("contract_hash"))
        return contract

    def _transition(self, task_id: str, phase: str, **changes: Any) -> Mapping[str, Any]:
        state = dict(self.state(task_id))
        state.update(changes)
        state.update({"phase": phase, "revision": int(state.get("revision", 0)) + 1, "updated_at": _now()})
        _atomic_json(self._state_path(task_id), state)
        self.store.append_event(task_id, "kernel.phase_changed", {"phase": phase, "state_revision": state["revision"]}, iteration=int(state.get("iteration", 0)))
        return state

    def _append_approval_record(
        self,
        task_id: str,
        *,
        approval_type: str,
        approve: bool,
        state: Optional[Mapping[str, Any]] = None,
        selected_capability_ids: Sequence[str] = (),
        selected_case_ids: Sequence[str] = (),
        selected_change_ids: Sequence[str] = (),
        user_feedback: Optional[str] = None,
    ) -> Mapping[str, Any]:
        state = state or self.state(task_id)
        root = self.store.task_dir(task_id) / "iterations" / ("iteration-%03d" % int(state.get("iteration", 0))) / "approvals"
        root.mkdir(parents=True, exist_ok=True)
        existing = sorted(root.glob("approval-record-*.json"))
        index = max([int(p.stem.rsplit("-", 1)[1]) for p in existing if p.stem.rsplit("-", 1)[-1].isdigit()] or [0]) + 1
        record = {
            "api_version": "aceval.approval-record/v1",
            "record_id": "%s:%03d" % (task_id, index),
            "task_id": task_id,
            "iteration": int(state.get("iteration", 0)),
            "approval_type": approval_type,
            "approve": bool(approve),
            "decision": state.get("decision"),
            "selected_capability_ids": list(selected_capability_ids),
            "selected_case_ids": list(selected_case_ids),
            "selected_change_ids": list(selected_change_ids),
            "user_feedback": user_feedback,
            "recorded_at": _now(),
        }
        path = root / ("approval-record-%03d.json" % index)
        _atomic_json(path, record)
        self.store.append_event(task_id, "user.approval_recorded", {"approval_record": str(path), "approval_type": approval_type, "approve": bool(approve)}, iteration=int(state.get("iteration", 0)))
        return record

    @staticmethod
    def _default_gateway(config: KernelConfig) -> SessionGateway:
        profile_path = config.trial_executor.profile_path if config.trial_executor and config.trial_executor.profile_path else config.remote_agent.profile_path
        if not profile_path:
            raise IterationKernelError("trial_executor.profile_path is required")
        profile = CatxAgentProfile.load(profile_path)
        configured = [(config.skill_repository, "/workspace/skill")]
        if config.code_repository:
            configured.append((config.code_repository, "/workspace/repo"))
        resources = []
        existing = {item.url: item for item in profile.repository_resources}
        for repository, default_mount in configured:
            fallback = existing.get(repository.ssh_url)
            token_env = repository.authorization_token_env or (fallback.authorization_token_env if fallback else None)
            if not token_env:
                raise IterationKernelError("remote repository authorization_token_env is required: %s" % repository.ssh_url)
            resources.append(
                CatxRepositoryResourceProfile(
                    url=repository.ssh_url,
                    authorization_token_env=token_env,
                    mount_path=repository.mount_path or (fallback.mount_path if fallback else default_mount),
                )
            )
        return CatxAgentClient(
            replace(profile, repository=None, repositories=tuple(resources)),
            binding_adapter=CatxPayloadBindingAdapter(),
        )

    @staticmethod
    def _default_model(config: KernelConfig) -> ModelClient:
        local = config.local_analysis
        model_environment = set(local.env_allowlist)
        if local.api_base_url_env:
            model_environment.add(local.api_base_url_env)
        if local.api_key_env:
            model_environment.add(local.api_key_env)
        # Development bridges are launched with ``python -m aceval.*``.  The
        # model subprocess receives a deliberately filtered environment, so
        # preserve only the already-configured package search path; without it
        # the isolated Claude bridge exits before making a real CC Switch call.
        if os.environ.get("PYTHONPATH"):
            model_environment.add("PYTHONPATH")
        return CommandModelClient(
            local.model_command,
            timeout_seconds=local.timeout_seconds,
            env_allowlist=tuple(sorted(model_environment)),
            max_request_bytes=max(64 * 1024, local.max_prompt_chars * 4),
            max_stdout_bytes=max(64 * 1024, local.max_output_chars * 4),
            model_id=local.model_id,
        )

    def create_task(
        self,
        config: KernelConfig,
        user_input: KernelInput,
        *,
        task_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        if task_id is None:
            slug = "".join(character if character in "abcdefghijklmnopqrstuvwxyz0123456789" else "-" for character in user_input.skill_name.lower()).strip("-")[:48] or "skill"
            task_id = "%s-%s" % (slug, uuid.uuid4().hex[:10])
        # Validate the identifier before using it in the managed checkout path.
        self.store.task_dir(task_id)
        managed_repositories = self.store.root / ".repositories"
        if managed_repositories.is_symlink():
            raise IterationKernelError("managed repository root cannot be a symlink")
        checkout_root = managed_repositories / task_id
        skill_repository = self.checkout_manager.prepare(config.skill_repository, checkout_root / "skill")
        code_repository = (
            self.checkout_manager.prepare(config.code_repository, checkout_root / "code")
            if config.code_repository is not None
            else None
        )
        config = replace(config, skill_repository=skill_repository, code_repository=code_repository)
        skill_file = _optional_skill_file(str(config.skill_repository.local_path))
        try:
            intent_mode = user_input.intent_mode(has_existing_skill=skill_file is not None)
        except Exception as exc:
            raise IterationKernelError(str(exc)) from exc
        if skill_file is not None:
            profile, _ = classify_evaluation(skill_file, "\n".join(user_input.effective_standards))
            scenario = profile.id
        else:
            scenario = "generic-skill-construction"
        task = self.store.create(
            task_id=task_id,
            skill_name=user_input.skill_name,
            skill_source=config.skill_repository.ssh_url + "@" + config.skill_repository.branch,
            scenario=scenario,
            goal=user_input.effective_goal,
            standards=user_input.effective_standards,
            cases=tuple(case.to_dict() for case in user_input.cases),
            environment={
                "execution": "remote-agent",
                "validation": "scenario-adapter",
                "skill_repository": config.skill_repository.to_dict(),
                "code_repository": config.code_repository.to_dict() if config.code_repository else None,
                "remote_profile": config.remote_agent.profile_path,
                "local_model_id": config.local_analysis.model_id,
            },
        )
        kernel_dir = self._kernel_dir(str(task["id"]))
        _atomic_json(kernel_dir / "config.json", config.to_dict())
        _atomic_json(kernel_dir / "input.json", user_input.to_dict())
        state = {
            "api_version": KERNEL_STATE_API_VERSION,
            "task_id": task["id"],
            "phase": "created",
            "revision": 1,
            "iteration": 0,
            "design_revision": 0,
            "active_batch": None,
            "brain_stage": None,
            "environment_contract": None,
            "environment_contract_hash": None,
            "decision": None,
            "candidate": None,
            "champion_commit": _git_head(config.skill_repository.local_path),
            "challenger_commit": None,
            "candidate_status": None,
            "intent_mode": intent_mode,
            "blueprint": None,
            "blueprint_approved": False,
            "design_approved": False,
            "approved_case_ids": [],
            "config_hash": contract_hash(config),
            "input_hash": contract_hash(user_input),
            "created_at": _now(),
            "updated_at": _now(),
        }
        _atomic_json(self._state_path(str(task["id"])), state)
        self.store.append_event(str(task["id"]), "kernel.created", {"input_mode": user_input.mode, "intent_mode": intent_mode, "config_hash": state["config_hash"], "input_hash": state["input_hash"]})
        return {"task": task, "state": state}

    def plan_capabilities(self, task_id: str) -> Mapping[str, Any]:
        """Plan extension, discovery, or greenfield construction before evaluation."""

        config = self._config(task_id)
        user_input = self._input(task_id)
        state = self.state(task_id)
        if state.get("phase") != "created" or state.get("blueprint_approved"):
            raise IterationKernelError("capability planning requires an unapproved created task")
        operation = str(state.get("intent_mode") or "auto")
        if operation not in ("extend", "discover", "create"):
            raise IterationKernelError("current Harness operation does not require a capability blueprint")
        root = _skill_root(str(config.skill_repository.local_path))
        policy = _optimization_policy(config)
        skill_file = _optional_skill_file(str(root))
        if skill_file is None:
            frozen = scan_skill_tree(root, policy, require_entrypoint=False)
            inventory = tuple(path for path in frozen if is_editable_skill_path(path, policy))
            current_skill = None
        else:
            inventory = editable_skill_inventory(root, policy)
            current_skill = skill_file.read_text(encoding="utf-8")
        try:
            blueprint = CapabilityArchitect(self.model_factory(config)).plan(
                operation=operation,
                user_input=user_input,
                existing_paths=inventory,
                current_skill=current_skill,
                policy=policy,
            )
        except SkillHarnessError as exc:
            raise IterationKernelError(str(exc)) from exc
        revision = int(state.get("blueprint_revision", 0)) + 1
        path = self._kernel_dir(task_id) / "blueprints" / ("revision-%03d.json" % revision)
        _atomic_json(path, blueprint)
        phase = "discovery_ready" if operation == "discover" else "blueprint_ready"
        self.store.append_event(task_id, "harness.blueprint_ready", {
            "operation": operation,
            "blueprint": str(path),
            "proposal_ids": [item.get("id") for item in blueprint.get("proposals", ()) if isinstance(item, Mapping)],
            "recommended_proposal_ids": list(blueprint.get("recommended_proposal_ids", ())),
        }, iteration=int(state.get("iteration", 0)))
        self.store.update(task_id, status="awaiting_confirmation")
        self._transition(task_id, phase, blueprint=str(path), blueprint_revision=revision)
        return blueprint

    def compile_design(self, task_id: str) -> Mapping[str, Any]:
        config = self._config(task_id)
        user_input = self._input(task_id)
        state = self.state(task_id)
        if state.get("phase") != "created":
            raise IterationKernelError("evaluation design can only be compiled from created")
        revision = int(state.get("design_revision", 0)) + 1
        root = self._kernel_dir(task_id) / "designs" / ("revision-%03d" % revision)
        custom_pack = None
        blueprint = self._blueprint(task_id)
        goal = str(blueprint.get("goal")) if blueprint and blueprint.get("selected_proposal_ids") else user_input.effective_goal
        standards = list(user_input.effective_standards)
        seeds = list(_default_seed(user_input))
        if blueprint and blueprint.get("selected_proposal_ids"):
            selected_ids = set(str(item) for item in blueprint.get("selected_proposal_ids", ()))
            for proposal in blueprint.get("proposals", ()):
                if not isinstance(proposal, Mapping) or str(proposal.get("id")) not in selected_ids:
                    continue
                standards.extend(str(item) for item in proposal.get("acceptance_criteria", ()) if isinstance(item, str))
            known_case_ids = {str(item.get("id")) for item in seeds if isinstance(item, Mapping)}
            for case in selected_blueprint_cases(blueprint):
                if str(case.get("id")) not in known_case_ids:
                    seeds.append(case)
                    known_case_ids.add(str(case.get("id")))
        standards = list(dict.fromkeys(standards))
        seeds = tuple(seeds)
        if user_input.evalpack_path:
            custom_pack, pack_seeds = _custom_evalpack_seed(user_input.evalpack_path)
            user_case_ids = {str(item.get("id")) for item in seeds}
            duplicate_ids = user_case_ids.intersection(str(item.get("id")) for item in pack_seeds)
            if duplicate_ids and user_input.cases:
                raise IterationKernelError("user Case ids conflict with custom EvalPack: %s" % ", ".join(sorted(duplicate_ids)))
            seeds = tuple(seeds) + tuple(pack_seeds) if (user_input.cases or (blueprint and blueprint.get("selected_proposal_ids"))) else tuple(pack_seeds)
        planning = create_planning_artifacts(
            _skill_file(str(config.skill_repository.local_path)),
            {"name": "%s-kernel" % user_input.skill_name, "version": "0.1.0", "cases": list(seeds)},
            goal,
            root / "planning",
            runtime_capabilities=tuple(sorted(set(REFERENCE_RUNTIME_CAPABILITIES).union(("network", "authentication", "browser", "screenshot", "remote_state_snapshot", "observability_tools", "approval_gate", "artifact_output")))),
            max_generated_cases=config.policy.max_generated_cases,
        )
        cases_document = dict(planning.case_generation.cases_document)
        raw_cases = cases_document.get("cases", ())
        cases = [dict(item) for item in raw_cases if isinstance(item, Mapping)]
        graph = planning.capability_graph.to_dict()
        capability_contract = contract_hash(graph)
        default_applicability = {
            "trial_provider": config.trial_executor.provider if config.trial_executor else "catx",
            "runtime_capabilities": sorted(set(REFERENCE_RUNTIME_CAPABILITIES).union(("network", "authentication", "browser", "screenshot", "remote_state_snapshot", "observability_tools", "approval_gate", "artifact_output"))),
        }
        cases = [
            dict(_normalize_case_identity(
                item,
                goal=goal,
                standards=standards,
                capability_contract=capability_contract,
                environment_applicability=default_applicability,
            ))
            for item in cases
        ]
        # Generated Case copy is model-driven through the selected CC Switch
        # profile. Deterministic planning remains the fail-closed fallback and
        # is explicitly surfaced in provenance when the model is unavailable.
        model_case_provenance = {
            "api_version": MODEL_CASE_DESIGN_API_VERSION,
            "strategy": "deterministic-fallback",
            "status": "not_requested",
            "model_id": str(config.local_analysis.model_id),
        }
        model_paths = None
        self.store.append_event(
            task_id,
            "evaluation.case_generation_started",
            {
                "case_count": len([item for item in cases if isinstance(item.get("metadata"), Mapping) and isinstance(item.get("metadata", {}).get("aceval_test"), Mapping) and item.get("metadata", {}).get("aceval_test", {}).get("origin") == "requirement_synthesis"]),
                "provider": "cc-switch",
                "model_id": config.local_analysis.model_id,
            },
            iteration=int(state.get("iteration", 0)),
        )
        try:
            skill_text = _skill_file(str(config.skill_repository.local_path)).read_text(encoding="utf-8")
            cases, model_case_provenance, model_paths = _model_generated_cases(
                model=self.model_factory(config),
                skill_text=skill_text,
                graph=graph,
                plan=planning.test_plan,
                goal=goal,
                standards=standards,
                seed_cases=seeds,
                cases=cases,
                artifact_root=root / "planning",
            )
            self.store.append_event(task_id, "evaluation.case_generation_completed" if model_case_provenance.get("status") == "validated" else "evaluation.case_generation_failed", dict(model_case_provenance), iteration=int(state.get("iteration", 0)))
        except Exception as exc:
            model_case_provenance = dict(model_case_provenance)
            model_case_provenance.update({"status": "fallback", "error": str(exc)[:500]})
            _atomic_json(root / "planning" / "model-case-generation.json", {"api_version": MODEL_CASE_DESIGN_API_VERSION, "provenance": model_case_provenance})
            self.store.append_event(task_id, "evaluation.case_generation_failed", {"provider": "cc-switch", "model_id": config.local_analysis.model_id, "error": str(exc)[:500], "fallback": True}, iteration=int(state.get("iteration", 0)))
        cases = [
            dict(_normalize_case_identity(
                item,
                goal=goal,
                standards=standards,
                capability_contract=capability_contract,
                environment_applicability=default_applicability,
            ))
            for item in cases
        ]
        source_ref_details = {}
        for capability in graph.get("capabilities", ()) if isinstance(graph.get("capabilities"), list) else ():
            if not isinstance(capability, Mapping):
                continue
            cap_id = str(capability.get("id") or "")
            for index, ref in enumerate(capability.get("source_refs", ()) if isinstance(capability.get("source_refs"), list) else ()):
                if isinstance(ref, Mapping):
                    source_ref_details["%s#source-%d" % (cap_id, index)] = dict(ref)
        for case in cases:
            metadata = dict(case.get("metadata", {})) if isinstance(case.get("metadata"), Mapping) else {}
            test = dict(metadata.get("aceval_test", {})) if isinstance(metadata.get("aceval_test"), Mapping) else {}
            refs = test.get("source_refs", ()) if isinstance(test.get("source_refs"), list) else ()
            test["source_ref_details"] = [source_ref_details[ref] for ref in refs if ref in source_ref_details]
            metadata["aceval_test"] = test
            case["metadata"] = metadata
        paths = {}
        for case in cases:
            case_id = str(case.get("id"))
            paths[case_id] = dict(model_paths[case_id]) if model_paths and case_id in model_paths else _path_for_case(case, graph)
            paths[case_id].setdefault("api_version", EXECUTION_PATH_SPEC_API_VERSION)
        # EvalPack builder accepts only its public case fields; identity fields
        # stay in the task design metadata/read model.
        pack_cases = []
        for item in cases:
            value = dict(item)
            for key in ("case_revision", "content_hash", "provenance", "environment_applicability", "reuse_key", "title"):
                value.pop(key, None)
            pack_cases.append(value)
        cases_document["cases"] = pack_cases
        _atomic_json(root / "execution-paths.json", {"api_version": "aceval.kernel-execution-paths/v1", "paths": paths})
        if custom_pack is None:
            pack_registry = self.store.root.parent / "packs"
            compilation = compile_evaluation(
                _skill_file(str(config.skill_repository.local_path)),
                goal,
                default_output_path(
                    _skill_file(str(config.skill_repository.local_path)),
                    goal,
                    standards="\n".join(standards),
                    root=pack_registry,
                ),
                standards="\n".join(standards),
                cases=cases_document,
                source_root=(
                    config.code_repository.local_path
                    if config.code_repository and config.code_repository.local_path
                    else config.skill_repository.local_path
                ),
                reuse_roots=(pack_registry,),
                reuse=True,
                test_design=planning.test_design(),
                allow_output_variant=True,
            )
            profile_value = compilation.profile.to_dict()
            evalpack_value = compilation.summary()
            pack_ref = str(compilation.pack.root)
            pack_source = compilation.source
        else:
            profile_value, _ = classify_evaluation(_skill_file(str(config.skill_repository.local_path)), "\n".join(standards))
            profile_value = profile_value.to_dict()
            pack_ref = str(custom_pack.root)
            pack_source = "custom"
            evalpack_value = {
                "api_version": "aceval.evaluation-compiler/v1",
                "status": "ready" if pack_calibration_status(custom_pack) in ("frozen", "legacy") else "evaluation_review_required",
                "source": "custom",
                "reused": True,
                "profile": profile_value,
                "signature": custom_pack.pack_hash,
                "lifecycle": pack_calibration_status(custom_pack),
                "required_inputs": [],
                "runtime_gaps": [],
                "artifacts": {"evaluation_suite": pack_ref, "experiment_plan": None},
            }
        design = {
            "api_version": EVALUATION_DESIGN_API_VERSION,
            "revision": revision,
            "task_id": task_id,
            "input_mode": user_input.mode,
            "intent_mode": state.get("intent_mode", user_input.operation),
            "goal": goal,
            "standards": standards,
            "blueprint_ref": state.get("blueprint"),
            "profile": profile_value,
            "cases": cases,
            "case_ids": [str(case.get("id")) for case in cases],
            "execution_paths": paths,
            "capability_summary": graph,
            "planning": planning.summary(),
            "test_design": planning.test_design(),
            "case_generation": model_case_provenance,
            "evalpack": evalpack_value,
            "source_subject_hash": graph.get("subject_hash"),
            "generated_at": _now(),
        }
        _atomic_json(root / "evaluation-design.json", design)
        _atomic_json(self._kernel_dir(task_id) / "active-design.json", design)
        task = self.store.load(task_id)
        evaluation = dict(task.get("evaluation", {}))
        evaluation.update({
            "mode": (
                "custom+automatic-supplement"
                if user_input.evalpack_path
                else ("automatic" if user_input.mode != "cases_with_expected" else "user-seeded+automatic")
            ),
            "status": "compiled",
            "cases": cases,
            "pack_ref": pack_ref,
            "pack_source": pack_source,
            "design_ref": str(root / "evaluation-design.json"),
            "execution_paths_ref": str(root / "execution-paths.json"),
            "customizable": True,
        })
        self.store.update(task_id, evaluation=evaluation)
        self.store.append_event(
            task_id,
            "evaluation.evalpack_ready",
            {
                "pack_ref": pack_ref,
                "pack_source": pack_source,
                "signature": evalpack_value.get("signature"),
                "lifecycle": evalpack_value.get("lifecycle"),
                "customizable": True,
            },
            iteration=int(state.get("iteration", 0)),
        )
        for case in cases:
            case_id = str(case.get("id"))
            path = paths.get(case_id, {})
            self.store.append_event(
                task_id,
                "evaluation.case_path_ready",
                {
                    "case_id": case_id,
                    "title": case.get("title") or (case.get("metadata", {}).get("aceval_test", {}).get("title") if isinstance(case.get("metadata"), Mapping) and isinstance(case.get("metadata", {}).get("aceval_test"), Mapping) else None),
                    "source": ((case.get("metadata") or {}).get("aceval_test", {}) or {}).get("origin") if isinstance(case.get("metadata"), Mapping) and isinstance((case.get("metadata") or {}).get("aceval_test"), Mapping) else ((case.get("metadata") or {}).get("source") if isinstance(case.get("metadata"), Mapping) else None),
                    "step_count": len(path.get("steps", ())) if isinstance(path, Mapping) else 0,
                    "path_api_version": path.get("api_version") if isinstance(path, Mapping) else None,
                    "kind": ((case.get("metadata") or {}).get("aceval_test", {}) or {}).get("kind") if isinstance(case.get("metadata"), Mapping) and isinstance((case.get("metadata") or {}).get("aceval_test"), Mapping) else None,
                    "source_refs": ((case.get("metadata") or {}).get("aceval_test", {}) or {}).get("source_refs", []) if isinstance(case.get("metadata"), Mapping) and isinstance((case.get("metadata") or {}).get("aceval_test"), Mapping) else [],
                    "generation_reason": ((case.get("metadata") or {}).get("aceval_test", {}) or {}).get("generation_reason") if isinstance(case.get("metadata"), Mapping) and isinstance((case.get("metadata") or {}).get("aceval_test"), Mapping) else None,
                },
                iteration=int(state.get("iteration", 0)),
                case_id=case_id,
            )
        evaluation_ready = evalpack_value.get("status") == "ready"
        self.store.append_event(task_id, "evaluation.design_compiled", {"revision": revision, "case_count": len(cases), "generated_case_count": len(planning.case_generation.generated_case_ids), "pack_ref": pack_ref, "pack_source": pack_source, "pending_oracle_case_ids": list(planning.case_generation.pending_oracle_case_ids), "evaluation_ready": evaluation_ready}, iteration=int(state.get("iteration", 0)))
        if not evaluation_ready:
            blocker = "Uncalibrated Cases may run for exploration, but cannot alone authorize a Skill mutation"
            self.store.append_event(
                task_id,
                "evaluation.design_review_required",
                {"reason": blocker, "pack_ref": pack_ref, "evalpack_status": evalpack_value.get("status")},
                iteration=int(state.get("iteration", 0)),
            )
        self._transition(task_id, "design_ready", design_revision=revision, active_design=str(root / "evaluation-design.json"), active_batch=None, design_approved=False, approved_case_ids=[], evaluation_blocker=None if evaluation_ready else blocker)
        return design

    def _design(self, task_id: str) -> Mapping[str, Any]:
        return _load_json(self._kernel_dir(task_id) / "active-design.json", "active evaluation design")

    def _coordinator(self, task_id: str) -> RemoteBatchCoordinator:
        config = self._config(task_id)
        remote = config.remote_agent
        return RemoteBatchCoordinator(
            self.gateway_factory(config), self.store,
            max_parallel=remote.max_parallel,
            poll_interval_seconds=remote.poll_interval_seconds,
            max_wait_seconds=remote.max_wait_seconds,
            max_prompt_chars=config.policy.max_remote_prompt_chars,
        )

    def _session_count(self, task_id: str) -> int:
        return sum(event.get("type") == "case_run.started" for event in self.store.events(task_id))

    def dispatch(
        self,
        task_id: str,
        *,
        purpose: str = "evaluation",
        case_ids: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        config = self._config(task_id)
        state = self.state(task_id)
        expected_phase = "verification_ready" if purpose == "pass-verification" else "evaluation_ready"
        if purpose not in ("evaluation", "pass-verification", "without-skill-baseline"):
            raise IterationKernelError("unsupported remote batch purpose")
        # Keep direct kernel callers backwards-compatible; automatic desktop
        # execution can only reach evaluation_ready through explicit review.
        allowed_phases = {expected_phase}
        if purpose in ("evaluation", "without-skill-baseline"):
            allowed_phases.add("design_ready")
        if state.get("phase") not in allowed_phases:
            raise IterationKernelError("remote batch %s requires phase %s" % (purpose, expected_phase))
        design = self._design(task_id)
        wanted = set(case_ids)
        if not wanted and purpose in ("evaluation", "without-skill-baseline"):
            wanted = set(str(item) for item in state.get("approved_case_ids", ()) if str(item))
        cases = [dict(case) for case in design.get("cases", ()) if isinstance(case, Mapping) and (not wanted or str(case.get("id")) in wanted)]
        if purpose == "without-skill-baseline":
            for case in cases:
                metadata = dict(case.get("metadata", {})) if isinstance(case.get("metadata"), Mapping) else {}
                metadata["harness_baseline"] = "without_skill"
                case["metadata"] = metadata
        if wanted != {str(case.get("id")) for case in cases} and wanted:
            raise IterationKernelError("dispatch references unknown case ids")
        if self._session_count(task_id) + len(cases) > config.policy.max_total_remote_sessions:
            raise IterationKernelError("remote session budget would be exceeded")
        environment_contract = self._ensure_environment_contract(
            task_id,
            design=design,
            state=state,
            iteration=int(state.get("iteration", 0)),
        )
        execution_binding = None
        coordinator = self._coordinator(task_id)
        if str(environment_contract.get("provider") or "catx") == "catx" and callable(getattr(coordinator.gateway, "verify_binding", None)):
            skill_contract = environment_contract.get("skill", {}) if isinstance(environment_contract.get("skill"), Mapping) else {}
            code_contract = environment_contract.get("code", {}) if isinstance(environment_contract.get("code"), Mapping) else {}
            binding_repo = config.code_repository if config.code_repository is not None else config.skill_repository
            repository_contract = code_contract if config.code_repository is not None else skill_contract
            repository_ref = str(repository_contract.get("ssh_url") or binding_repo.ssh_url or "").strip()
            if repository_ref:
                try:
                    execution_binding = CatxExecutionBinding(
                        api_version=CATX_EXECUTION_BINDING_API_VERSION,
                        subject_hash=str(design.get("source_subject_hash") or design.get("capability_summary", {}).get("subject_hash")),
                        skill_ref=str(skill_contract.get("ssh_url") or config.skill_repository.ssh_url or ""),
                        repository_ref=repository_ref,
                        repository_hash=repository_contract.get("working_tree_hash"),
                        base_commit=repository_contract.get("revision"),
                        head_commit=repository_contract.get("revision"),
                        metadata={
                            "task_id": task_id,
                            "iteration": int(state.get("iteration", 0)),
                            "code_repository": environment_contract.get("code", {}),
                            "expected_mounts": [
                                {
                                    "mount_path": config.skill_repository.mount_path or "/workspace/skill",
                                    "revision": skill_contract.get("revision"),
                                },
                                *([{
                                    "mount_path": config.code_repository.mount_path or "/workspace/repo",
                                    "revision": code_contract.get("revision"),
                                }] if config.code_repository is not None else []),
                            ],
                        },
                    )
                except Exception as exc:
                    raise IterationKernelError("cannot construct immutable CATX repository binding: %s" % exc) from exc
        batch = coordinator.dispatch(
            task_id,
            int(state.get("iteration", 0)),
            purpose,
            cases,
            goal=str(design.get("goal") or self._input(task_id).effective_goal),
            standards=tuple(str(item) for item in design.get("standards", self._input(task_id).effective_standards)),
            repository_bindings=tuple(
                item
                for item in (
                    {
                        "role": "skill",
                        "branch": config.skill_repository.branch,
                        "mount_path": config.skill_repository.mount_path or "/workspace/skill",
                        "revision": state.get("challenger_commit") or state.get("champion_commit") or state.get("candidate_commit"),
                    },
                    (
                        {
                            "role": "code",
                            "branch": config.code_repository.branch,
                            "mount_path": config.code_repository.mount_path or "/workspace/repo",
                        }
                        if config.code_repository
                        else None
                    ),
                )
                if item is not None
            ),
            environment_contract_hash=str(environment_contract["contract_hash"]),
            execution_binding=execution_binding,
        )
        phase = {
            "pass-verification": "verification_running",
            "without-skill-baseline": "baseline_running",
            "evaluation": "remote_running",
        }[purpose]
        self.store.update(task_id, status="running")
        self._transition(task_id, phase, active_batch=purpose)
        return batch

    def collect(self, task_id: str, *, wait: bool = False) -> Mapping[str, Any]:
        state = self.state(task_id)
        if state.get("phase") not in ("remote_running", "verification_running", "baseline_running"):
            raise IterationKernelError("log collection requires a running remote batch")
        purpose = str(state.get("active_batch") or "")
        if not purpose:
            raise IterationKernelError("no active remote batch")
        coordinator = self._coordinator(task_id)
        if wait:
            batch = coordinator.wait(task_id, int(state.get("iteration", 0)), purpose)
        else:
            batch = coordinator.collect_once(task_id, int(state.get("iteration", 0)), purpose)
        if batch.get("status") == "completed":
            phase = {
                "pass-verification": "verification_collected",
                "without-skill-baseline": "baseline_collected",
                "evaluation": "remote_collected",
            }[purpose]
            self._transition(task_id, phase)
        return batch

    def retry_failed(self, task_id: str, *, purpose: str) -> Mapping[str, Any]:
        if purpose not in ("evaluation", "pass-verification", "without-skill-baseline"):
            raise IterationKernelError("unsupported remote batch purpose")
        state = self.state(task_id)
        execution_binding = None
        if state.get("environment_contract"):
            try:
                environment_contract = _load_json(Path(str(state["environment_contract"])), "frozen trial environment contract")
                coordinator = self._coordinator(task_id)
                if str(environment_contract.get("provider") or "catx") == "catx" and callable(getattr(coordinator.gateway, "verify_binding", None)):
                    skill_contract = environment_contract.get("skill", {}) if isinstance(environment_contract.get("skill"), Mapping) else {}
                    code_contract = environment_contract.get("code", {}) if isinstance(environment_contract.get("code"), Mapping) else {}
                    binding_repo = self._config(task_id).code_repository if self._config(task_id).code_repository is not None else self._config(task_id).skill_repository
                    repository_contract = code_contract if self._config(task_id).code_repository is not None else skill_contract
                    repository_ref = str(repository_contract.get("ssh_url") or binding_repo.ssh_url or "").strip()
                    execution_binding = CatxExecutionBinding(
                        api_version=CATX_EXECUTION_BINDING_API_VERSION,
                        subject_hash=str(self._design(task_id).get("source_subject_hash") or self._design(task_id).get("capability_summary", {}).get("subject_hash")),
                        skill_ref=str(skill_contract.get("ssh_url") or self._config(task_id).skill_repository.ssh_url or ""),
                        repository_ref=repository_ref,
                        repository_hash=repository_contract.get("working_tree_hash"),
                        base_commit=repository_contract.get("revision"),
                        head_commit=repository_contract.get("revision"),
                        metadata={
                            "task_id": task_id,
                            "iteration": int(state.get("iteration", 0)),
                            "code_repository": code_contract,
                            "expected_mounts": [
                                {
                                    "mount_path": self._config(task_id).skill_repository.mount_path or "/workspace/skill",
                                    "revision": skill_contract.get("revision"),
                                },
                                *([{
                                    "mount_path": self._config(task_id).code_repository.mount_path or "/workspace/repo",
                                    "revision": code_contract.get("revision"),
                                }] if self._config(task_id).code_repository is not None else []),
                            ],
                        },
                    )
            except Exception as exc:
                raise IterationKernelError("cannot reconstruct immutable CATX repository binding for retry: %s" % exc) from exc
        try:
            batch = self._coordinator(task_id).retry_failed(
                task_id, int(state.get("iteration", 0)), purpose, execution_binding=execution_binding
            )
        except RemoteBatchError as exc:
            raise IterationKernelError(str(exc)) from exc
        phase = {
            "evaluation": "remote_running",
            "pass-verification": "verification_running",
            "without-skill-baseline": "baseline_running",
        }[purpose]
        self.store.update(task_id, status="running")
        self._transition(task_id, phase, active_batch=purpose, decision=None, brain_stage=None)
        return batch

    def _batch(self, task_id: str, purpose: str) -> Mapping[str, Any]:
        state = self.state(task_id)
        return _load_json(self._coordinator(task_id).batch_path(task_id, int(state.get("iteration", 0)), purpose), "remote batch")

    def analyze(self, task_id: str) -> Mapping[str, Any]:
        config = self._config(task_id)
        user_input = self._input(task_id)
        state = self.state(task_id)
        if state.get("phase") not in (
            "remote_collected",
            "verification_collected",
            "evidence_ready",
            "semantic_grading",
            "attribution",
            "proposal",
        ):
            raise IterationKernelError("analysis requires a fully collected remote batch")
        if config.analysis_executor and config.analysis_executor.provider != "local-forge":
            raise IterationKernelError("analysis_executor provider is unsupported; P0 only supports local-forge")
        design = self._design(task_id)
        approved_case_ids = set(str(item) for item in state.get("approved_case_ids", ()) if str(item))
        analysis_cases = tuple(
            item
            for item in design.get("cases", ())
            if isinstance(item, Mapping)
            and (not approved_case_ids or str(item.get("id")) in approved_case_ids)
        )
        primary = self._batch(task_id, "evaluation")
        verification_path = self._coordinator(task_id).batch_path(task_id, int(state.get("iteration", 0)), "pass-verification")
        verification = None
        if verification_path.is_file():
            candidate_verification = _load_json(verification_path, "verification batch")
            if candidate_verification.get("status") == "completed":
                verification = candidate_verification
        baseline_path = self._coordinator(task_id).batch_path(task_id, int(state.get("iteration", 0)), "without-skill-baseline")
        comparison_baseline = None
        if baseline_path.is_file():
            candidate_baseline = _load_json(baseline_path, "without-Skill baseline batch")
            if candidate_baseline.get("status") == "completed":
                comparison_baseline = candidate_baseline
        contract_path = self._environment_contract_path(task_id, int(state.get("iteration", 0)))
        if not contract_path.is_file():
            raise IterationKernelError("analysis requires a frozen trial environment contract")
        try:
            environment_contract = _load_json(contract_path, "frozen trial environment contract")
            expected_environment_hash = verify_contract_hash(environment_contract)
        except TrialEnvironmentContractError as exc:
            raise IterationKernelError(str(exc)) from exc
        if state.get("environment_contract_hash") not in (None, expected_environment_hash):
            raise IterationKernelError("state environment contract does not match frozen contract")
        compared_batches = (primary,) + ((verification,) if verification is not None else ()) + ((comparison_baseline,) if comparison_baseline is not None else ())
        environment_hashes = {str(item.get("environment_contract_hash")) for item in compared_batches}
        if environment_hashes != {expected_environment_hash}:
            raise IterationKernelError("evaluation and pass verification must use the same frozen environment contract")
        model = self.model_factory(config)
        graph = design.get("capability_summary", {}) if isinstance(design.get("capability_summary"), Mapping) else {}
        skill_root = _skill_root(str(config.skill_repository.local_path))
        resource_inventory = list(editable_skill_inventory(skill_root, _optimization_policy(config)))
        blueprint = self._blueprint(task_id)
        if blueprint:
            for item in selected_file_plan(blueprint):
                if item.get("action") == "create" and item.get("path") not in resource_inventory:
                    resource_inventory.append(str(item.get("path")))
        iteration_artifacts = self.store.task_dir(task_id) / "iterations" / ("iteration-%03d" % int(state.get("iteration", 0))) / "analysis"
        self._transition(task_id, "evidence_ready", evidence_bundle=str(iteration_artifacts / "evidence-bundle.json"))
        decision = dict(IterationBrain(
            model,
            iteration_artifacts,
            provider=(config.analysis_executor.provider if config.analysis_executor else "local-forge"),
            model_id=config.local_analysis.model_id,
            max_retries=0,
            max_prompt_chars=config.local_analysis.max_prompt_chars,
            max_evidence_chars_per_case=config.policy.max_analysis_evidence_chars_per_case,
            stage_callback=lambda stage: self._transition(task_id, stage, brain_stage=stage),
        ).analyze(
            task_id=task_id,
            iteration=int(state.get("iteration", 0)),
            goal=str(design.get("goal") or user_input.effective_goal),
            standards=tuple(str(item) for item in design.get("standards", user_input.effective_standards)),
            cases=analysis_cases,
            primary_batch=primary,
            path_specs=design.get("execution_paths", {}),
            verification_batch=verification,
            comparison_baseline_batch=comparison_baseline,
            capability_summary=graph,
            editable_resource_inventory=resource_inventory,
        ))
        case_count = max(1, len(analysis_cases))
        score = len(decision["stable_pass_case_ids"]) / case_count
        prior_decisions = []
        for prior_path in sorted((self.store.task_dir(task_id) / "iterations").glob("iteration-*/analysis-decision.json")):
            try:
                prior = _load_json(prior_path, "previous analysis decision")
            except IterationKernelError:
                continue
            if int(prior.get("iteration", -1)) < int(state.get("iteration", 0)):
                prior_decisions.append(prior)
        prior_scores = [
            float(item["quality_score"])
            for item in prior_decisions
            if isinstance(item.get("quality_score"), (int, float)) and not isinstance(item.get("quality_score"), bool)
        ]
        previous_score = prior_scores[-1] if prior_scores else None
        improvement = score - previous_score if previous_score is not None else None
        decision["quality_score"] = score
        decision["improvement_from_previous"] = improvement
        decision["round_number"] = int(state.get("iteration", 0)) + 1
        decision["evaluation_design_hash"] = contract_hash(design)

        comparison = None
        previous_decision = prior_decisions[-1] if prior_decisions else None
        challenger_commit = state.get("challenger_commit")
        champion_commit = state.get("champion_commit")
        if (
            verification is not None
            and previous_decision is not None
            and isinstance(previous_decision.get("case_aggregates"), list)
            and isinstance(decision.get("case_aggregates"), list)
            and challenger_commit
            and champion_commit
        ):
            comparison = compare_candidates(
                decision["case_aggregates"],
                previous_decision["case_aggregates"],
                champion_id=str(champion_commit),
                challenger_id=str(challenger_commit),
                minimum_effect=float(config.policy.min_improvement),
            )
        decision["candidate_comparison"] = comparison
        if (
            state.get("intent_mode") == "create"
            and comparison_baseline is not None
            and decision.get("next_action") == "converged"
            and not decision.get("incremental_value_case_ids")
        ):
            decision["next_action"] = "needs_evidence"
            decision["intervention_blocker"] = "new Skill does not yet demonstrate measurable value over the without-Skill baseline"
            decision["requires_user_confirmation"] = False
        recent_graph_hashes = [
            str(item.get("diagnosis_graph", {}).get("graph_hash"))
            for item in prior_decisions
            if isinstance(item.get("diagnosis_graph"), Mapping) and item.get("diagnosis_graph", {}).get("graph_hash")
        ]
        if isinstance(decision.get("diagnosis_graph"), Mapping) and decision["diagnosis_graph"].get("graph_hash"):
            recent_graph_hashes.append(str(decision["diagnosis_graph"]["graph_hash"]))
        recent_effects = [
            float(item.get("candidate_comparison", {}).get("paired_mean_delta"))
            for item in prior_decisions
            if isinstance(item.get("candidate_comparison"), Mapping)
            and isinstance(item.get("candidate_comparison", {}).get("paired_mean_delta"), (int, float))
        ]
        if isinstance(comparison, Mapping):
            recent_effects.append(float(comparison.get("paired_mean_delta", 0.0)))
        convergence = dict(convergence_state(
            case_aggregates=decision.get("case_aggregates", ()),
            failed_case_ids=decision["failed_case_ids"],
            comparison=comparison,
            round_number=decision["round_number"],
            max_rounds=config.policy.max_rounds,
            recent_graph_hashes=recent_graph_hashes,
            recent_effects=recent_effects,
            patience=config.policy.convergence_patience,
            minimum_effect=float(config.policy.min_improvement),
            budget_exhausted=self._session_count(task_id) >= config.policy.max_total_remote_sessions,
        ))
        if convergence.get("reason") == "target_met":
            convergence["reason"] = "quality_target_met"
        elif convergence.get("reason") == "no_material_gain":
            convergence["reason"] = "improvement_plateau"
        convergence.update({
            "minimum_improvement": config.policy.min_improvement,
            "patience": config.policy.convergence_patience,
        })
        if decision["next_action"] == "converged" and not convergence.get("converged"):
            # Preserve the semantic analyzer's all-green signal only when the
            # deterministic aggregate agrees it is stable.
            decision["next_action"] = "needs_evidence"
            decision["intervention_blocker"] = "semantic pass is missing required stable, valid evidence"
            decision["requires_user_confirmation"] = False
        elif decision["next_action"] == "await_user_confirmation" and convergence.get("converged"):
            decision["next_action"] = "converged"
            decision["requires_user_confirmation"] = False
        decision["convergence"] = convergence

        transition_changes: dict[str, Any] = {}
        if isinstance(comparison, Mapping):
            if comparison.get("accepted") is True:
                transition_changes.update({
                    "champion_commit": challenger_commit,
                    "challenger_commit": None,
                    "candidate_status": "promoted",
                })
                decision["candidate_outcome"] = {"status": "promoted", "commit": challenger_commit}
                self.store.append_event(task_id, "candidate.promoted", {"comparison": comparison}, iteration=int(state.get("iteration", 0)))
            else:
                restored_commit = None
                restoration_status = "publisher_does_not_manage_git"
                if hasattr(self.publisher, "restore_rejected_candidate"):
                    try:
                        restored_commit = self.publisher.restore_rejected_candidate(
                            config.skill_repository,
                            challenger_commit=str(challenger_commit),
                            champion_commit=str(champion_commit),
                        )
                        restoration_status = "restored"
                    except Exception as exc:
                        restoration_status = "failed"
                        decision["next_action"] = "needs_evidence"
                        decision["requires_user_confirmation"] = False
                        decision["intervention_blocker"] = "rejected candidate could not be restored: %s" % exc
                transition_changes.update({
                    "challenger_commit": None if restoration_status != "failed" else challenger_commit,
                    "candidate_status": "rejected" if restoration_status != "failed" else "rejection_restore_failed",
                    "restored_commit": restored_commit,
                })
                decision["candidate_outcome"] = {
                    "status": "rejected",
                    "commit": challenger_commit,
                    "champion_commit": champion_commit,
                    "restoration_status": restoration_status,
                    "restored_commit": restored_commit,
                }
                self.store.append_event(task_id, "candidate.rejected", {"comparison": comparison, "restoration_status": restoration_status, "restored_commit": restored_commit}, iteration=int(state.get("iteration", 0)))
        decision_path = self.store.task_dir(task_id) / "iterations" / ("iteration-%03d" % int(state.get("iteration", 0))) / "analysis-decision.json"
        _atomic_json(decision_path, decision)
        next_action = str(decision["next_action"])
        phase = {
            "verify_passes": "verification_ready",
            "needs_evidence": "needs_evidence",
            "await_user_confirmation": "awaiting_confirmation",
            "converged": "converged",
        }[next_action]
        self.store.append_event(task_id, "analysis.completed", {"decision": str(decision_path), "next_action": next_action, "failed_case_ids": decision["failed_case_ids"], "stable_pass_case_ids": decision["stable_pass_case_ids"], "flaky_case_ids": decision["flaky_case_ids"], "token_economy": decision["token_economy"]}, iteration=int(state.get("iteration", 0)))
        if phase == "awaiting_confirmation":
            self.store.append_event(task_id, "optimization.proposal_ready", {"proposed_changes": decision["proposed_changes"], "target_scope": decision["target_scope"], "conflicts": decision["conflicts"], "decision": str(decision_path)}, iteration=int(state.get("iteration", 0)))
        if phase == "converged":
            reason = {
                "quality_target_met": "all cases passed and passed cases were stable on verification",
                "maximum_rounds_reached": "configured maximum optimization rounds reached",
                "improvement_plateau": "quality improvement remained below the configured threshold",
            }.get(str(decision["convergence"].get("reason")), "iteration loop converged")
            self.store.record_decision(task_id, iteration=int(state.get("iteration", 0)), decision="converged", reason=reason, metrics={"quality_score": score, "stable_pass_cases": len(decision["stable_pass_case_ids"]), "remaining_failed_cases": len(decision["failed_case_ids"])})
        else:
            self.store.update(task_id, status="awaiting_confirmation" if phase == "awaiting_confirmation" else ("blocked" if phase == "needs_evidence" else "running"))
        self._transition(task_id, phase, decision=str(decision_path), approved_decision=None, active_batch=None, **transition_changes)
        return decision

    def confirm(
        self,
        task_id: str,
        *,
        approve: bool,
        supplement: Optional[KernelInput] = None,
        selected_capability_ids: Sequence[str] = (),
        selected_case_ids: Sequence[str] = (),
        selected_change_ids: Sequence[str] = (),
        user_feedback: Optional[str] = None,
    ) -> Mapping[str, Any]:
        state = self.state(task_id)
        if supplement is not None:
            if state.get("phase") not in ("awaiting_confirmation", "needs_evidence", "blocked", "blueprint_ready", "discovery_ready", "initial_candidate_ready"):
                raise IterationKernelError("requirements can only be supplemented at a user-input gate")
            archive = self._kernel_dir(task_id) / "input-history" / ("revision-%03d.json" % int(state.get("revision", 0)))
            _atomic_json(archive, self._input(task_id).to_dict())
            _atomic_json(self._kernel_dir(task_id) / "input.json", supplement.to_dict())
            has_existing = _optional_skill_file(str(self._config(task_id).skill_repository.local_path)) is not None
            try:
                intent_mode = supplement.intent_mode(has_existing_skill=has_existing)
            except Exception as exc:
                raise IterationKernelError(str(exc)) from exc
            had_trial = bool(state.get("environment_contract") or state.get("active_batch") or state.get("decision"))
            next_iteration = int(state.get("iteration", 0)) + (1 if had_trial else 0)
            self.store.append_event(task_id, "user.requirements_supplemented", {"input_hash": contract_hash(supplement), "mode": supplement.mode, "intent_mode": intent_mode}, iteration=int(state.get("iteration", 0)))
            if had_trial:
                self.store.update(task_id, current_iteration=next_iteration, status="running")
            self._transition(
                task_id,
                "created",
                iteration=next_iteration,
                input_hash=contract_hash(supplement),
                intent_mode=intent_mode,
                blueprint=None,
                blueprint_approved=False,
                design_approved=False,
                approved_case_ids=[],
                active_batch=None,
                brain_stage=None,
                environment_contract=None,
                environment_contract_hash=None,
                decision=None,
                approved_decision=None,
                candidate=None,
            )
            return self.advance(task_id)
        if state.get("phase") in ("blueprint_ready", "discovery_ready"):
            self._append_approval_record(task_id, approval_type="capability_blueprint", approve=approve, state=state, selected_capability_ids=selected_capability_ids)
            if not approve:
                self.store.record_decision(task_id, iteration=int(state.get("iteration", 0)), decision="blocked", reason="user rejected the capability blueprint")
                return self._transition(task_id, "blocked")
            blueprint = self._blueprint(task_id)
            if blueprint is None:
                raise IterationKernelError("capability blueprint is missing")
            try:
                if state.get("phase") == "discovery_ready":
                    blueprint = select_blueprint(blueprint, selected_capability_ids)
                elif selected_capability_ids:
                    blueprint = select_blueprint(blueprint, selected_capability_ids)
            except SkillHarnessError as exc:
                raise IterationKernelError(str(exc)) from exc
            approved_path = self._kernel_dir(task_id) / "blueprints" / ("approved-%03d.json" % int(state.get("blueprint_revision", 1)))
            _atomic_json(approved_path, blueprint)
            operation = str(blueprint.get("operation"))
            phase = "ready_to_build" if operation == "create" else "created"
            self.store.append_event(task_id, "user.capability_blueprint_approved", {"operation": operation, "selected_proposal_ids": list(blueprint.get("selected_proposal_ids", ())), "blueprint": str(approved_path)}, iteration=int(state.get("iteration", 0)))
            self.store.update(task_id, status="running")
            return self._transition(task_id, phase, blueprint=str(approved_path), blueprint_approved=True, intent_mode=operation)
        if state.get("phase") == "design_ready":
            design = self._design(task_id)
            if user_feedback is not None:
                if not isinstance(user_feedback, str) or not user_feedback.strip() or len(user_feedback) > 16_000 or "\x00" in user_feedback:
                    raise IterationKernelError("user feedback must be non-empty text up to 16000 characters")
                user_feedback = user_feedback.strip()
            known = tuple(str(item.get("id")) for item in design.get("cases", ()) if isinstance(item, Mapping) and item.get("id"))
            selected = tuple(dict.fromkeys(str(item) for item in selected_case_ids if str(item))) or known
            unknown = sorted(set(selected).difference(known))
            if unknown:
                raise IterationKernelError("selected evaluation cases are unknown: %s" % ", ".join(unknown))
            self._append_approval_record(
                task_id,
                approval_type="evaluation_design",
                approve=approve,
                state=state,
                selected_case_ids=selected,
                user_feedback=user_feedback,
            )
            if not approve:
                self.store.record_decision(task_id, iteration=int(state.get("iteration", 0)), decision="blocked", reason="user rejected the evaluation design")
                return self._transition(task_id, "blocked", design_approved=False)
            if not selected:
                raise IterationKernelError("at least one evaluation case must be approved")
            self.store.append_event(
                task_id,
                "user.evaluation_design_approved",
                {"selected_case_ids": list(selected), "has_feedback": user_feedback is not None},
                iteration=int(state.get("iteration", 0)),
            )
            self.store.update(task_id, status="running")
            return self._transition(task_id, "evaluation_ready", design_approved=True, approved_case_ids=list(selected))
        if state.get("phase") == "initial_candidate_ready":
            self._append_approval_record(task_id, approval_type="initial_candidate", approve=approve, state=state)
            if not approve:
                self.store.record_decision(task_id, iteration=0, decision="blocked", reason="user rejected the generated initial Skill candidate")
                return self._transition(task_id, "blocked")
            self.store.append_event(
                task_id,
                "user.initial_candidate_approved",
                {"candidate": state.get("candidate")},
                iteration=0,
            )
            self.store.update(task_id, status="running")
            return self._transition(task_id, "candidate_ready")
        if not approve:
            if state.get("phase") != "awaiting_confirmation":
                raise IterationKernelError("change-scope rejection requires awaiting_confirmation")
            self._append_approval_record(task_id, approval_type="change_scope", approve=False, state=state)
            self.store.record_decision(task_id, iteration=int(state.get("iteration", 0)), decision="blocked", reason="user rejected the proposed optimization scope")
            return self._transition(task_id, "blocked")
        if state.get("phase") != "awaiting_confirmation":
            raise IterationKernelError("change-scope approval requires awaiting_confirmation")
        decision_path = Path(str(state.get("decision")))
        decision = dict(_load_json(decision_path, "analysis decision"))
        if user_feedback is not None:
            if not isinstance(user_feedback, str) or not user_feedback.strip() or len(user_feedback) > 16_000 or "\x00" in user_feedback:
                raise IterationKernelError("user feedback must be non-empty text up to 16000 characters")
            decision["user_feedback"] = user_feedback.strip()
        selected = tuple(dict.fromkeys(str(item) for item in selected_change_ids if str(item)))
        if selected:
            graph = decision.get("diagnosis_graph", {}) if isinstance(decision.get("diagnosis_graph"), Mapping) else {}
            proposals = {
                str(item.get("id")): item
                for item in graph.get("proposals", ())
                if isinstance(item, Mapping) and item.get("id")
            }
            unknown = sorted(set(selected).difference(proposals))
            if unknown:
                raise IterationKernelError("selected optimization proposals are unknown: %s" % ", ".join(unknown))
            approved = [dict(proposals[item]) for item in selected]
            decision["approved_change_ids"] = list(selected)
            decision["proposed_changes"] = approved
            decision["target_scope"] = list(dict.fromkeys(str(item.get("target")) for item in approved if item.get("target")))
            if not decision["target_scope"]:
                raise IterationKernelError("selected optimization proposals have no editable target")
        self._append_approval_record(task_id, approval_type="change_scope", approve=True, state=state, selected_change_ids=selected, user_feedback=user_feedback)
        # The analysis decision is immutable evidence.  User choices live in
        # an append-only overlay consumed by optimize().
        overlay_root = decision_path.parent / "approved-overlays"
        overlays = sorted(overlay_root.glob("decision-overlay-*.json"))
        overlay_index = max([int(p.stem.rsplit("-", 1)[1]) for p in overlays if p.stem.rsplit("-", 1)[-1].isdigit()] or [0]) + 1
        overlay_path = overlay_root / ("decision-overlay-%03d.json" % overlay_index)
        _atomic_json(overlay_path, decision)
        self.store.append_event(
            task_id,
            "user.change_scope_approved",
            {
                "decision": state.get("decision"),
                "approved_overlay": str(overlay_path),
                "selected_change_ids": list(selected),
                "has_feedback": user_feedback is not None,
            },
            iteration=int(state.get("iteration", 0)),
        )
        self.store.update(task_id, status="running")
        return self._transition(task_id, "ready_to_optimize", approved_decision=str(overlay_path))

    def build_initial_candidate(self, task_id: str) -> Mapping[str, Any]:
        """Build a greenfield Skill only after its capability blueprint is approved."""

        config = self._config(task_id)
        state = self.state(task_id)
        if state.get("phase") != "ready_to_build" or state.get("intent_mode") != "create":
            raise IterationKernelError("initial Skill construction requires an approved create blueprint")
        blueprint = self._blueprint(task_id)
        if blueprint is None:
            raise IterationKernelError("approved create blueprint is missing")
        output = self.store.task_dir(task_id) / "iterations" / "iteration-000" / "candidates"
        candidate = SkillProjectBuilder(self.model_factory(config)).build(
            subject=_skill_root(str(config.skill_repository.local_path)),
            blueprint=blueprint,
            skill_name=self._input(task_id).skill_name,
            output_root=output,
            policy=_optimization_policy(config),
        )
        payload = {
            "snapshot_id": candidate.snapshot_id,
            "path": str(candidate.path),
            "subject_hash": candidate.subject_hash,
            "parent_hash": candidate.parent_hash,
            "patch": candidate.patch,
            "rationale": candidate.rationale,
            "usage": dict(candidate.usage or {}),
            "changed_paths": list(candidate.changed_paths),
            "created_paths": list(candidate.created_paths),
            "validation": list(candidate.validation),
            "initial_build": True,
            "publish_required": True,
        }
        _atomic_json(output / "candidate.json", payload)
        self.store.append_event(task_id, "harness.initial_candidate_created", {"candidate": str(output / "candidate.json"), "subject_hash": candidate.subject_hash, "created_paths": list(candidate.created_paths), "validation": list(candidate.validation)}, iteration=0)
        # Blueprint approval authorizes construction, not publication.  Keep the
        # exact generated tree behind a second review gate before touching Git.
        self.store.update(task_id, status="awaiting_confirmation")
        self._transition(task_id, "initial_candidate_ready", candidate=str(output / "candidate.json"))
        return payload

    def optimize(self, task_id: str) -> Mapping[str, Any]:
        config = self._config(task_id)
        state = self.state(task_id)
        if state.get("phase") != "ready_to_optimize":
            raise IterationKernelError("optimization requires an approved change scope")
        decision = _load_json(Path(str(state.get("approved_decision") or state.get("decision"))), "approved analysis decision")
        failures = tuple(
            FailureEvidence(
                scenario_id=str(cluster.get("id") or "failure-cluster"),
                grader_id="cross_case_analysis_v1",
                summary=str(cluster.get("root_cause") or "failed case cluster"),
                evidence=(
                    {"case_ids": list(cluster.get("case_ids", ())), "proposed_changes": decision.get("proposed_changes", ()), "conflicts": decision.get("conflicts", ()), "approved_target_scope": decision.get("target_scope", ()), "user_feedback": decision.get("user_feedback")},
                ),
            )
            for cluster in decision.get("failure_clusters", ())
            if isinstance(cluster, Mapping) and cluster.get("skill_change_authorized", True)
        )
        if not failures:
            raise IterationKernelError("analysis did not authorize a Skill change")
        output = self.store.task_dir(task_id) / "iterations" / ("iteration-%03d" % int(state.get("iteration", 0))) / "candidates"
        blueprint = self._blueprint(task_id)
        create_paths = tuple(
            item["path"] for item in (selected_file_plan(blueprint) if blueprint else ())
            if item.get("action") == "create" and not (_skill_root(str(config.skill_repository.local_path)) / item["path"]).exists()
        )
        candidate = SkillTreeOptimizer(self.model_factory(config)).propose(
            _skill_root(str(config.skill_repository.local_path)),
            failures,
            output,
            target_scope=tuple(str(item) for item in decision.get("target_scope", ())),
            allow_create_paths=create_paths,
            policy=_optimization_policy(config),
            forbidden_literals=_test_only_literals(
                tuple(item for item in self._design(task_id).get("cases", ()) if isinstance(item, Mapping))
            ),
        )
        payload = {
            "snapshot_id": candidate.snapshot_id,
            "path": str(candidate.path),
            "subject_hash": candidate.subject_hash,
            "parent_hash": candidate.parent_hash,
            "patch": candidate.patch,
            "rationale": candidate.rationale,
            "usage": dict(candidate.usage or {}),
            "changed_paths": list(candidate.changed_paths),
            "created_paths": list(candidate.created_paths),
            "validation": list(candidate.validation),
            "publish_required": True,
        }
        _atomic_json(output / "candidate.json", payload)
        self.store.append_event(task_id, "candidate.created", {"candidate": str(output / "candidate.json"), "subject_hash": candidate.subject_hash, "parent_hash": candidate.parent_hash, "rationale": candidate.rationale, "changed_paths": list(candidate.changed_paths), "created_paths": list(candidate.created_paths), "validation": list(candidate.validation), "usage": dict(candidate.usage or {})}, iteration=int(state.get("iteration", 0)))
        self._transition(task_id, "candidate_ready", candidate=str(output / "candidate.json"))
        return payload

    def publish_candidate(self, task_id: str) -> Mapping[str, Any]:
        """Publish only after the user-confirmation gate has been recorded."""

        config = self._config(task_id)
        state = self.state(task_id)
        if state.get("phase") != "candidate_ready":
            raise IterationKernelError("candidate publication requires candidate_ready")
        candidate = _load_json(Path(str(state.get("candidate"))), "candidate")
        commit = self.publisher.publish(
            config.skill_repository,
            Path(str(candidate.get("path"))),
            message=(
                "aceval: create %s" % self._input(task_id).skill_name
                if candidate.get("initial_build")
                else "aceval: optimize %s iteration %s" % (self._input(task_id).skill_name, state.get("iteration", 0))
            ),
        )
        if candidate.get("initial_build"):
            self.store.append_event(task_id, "harness.initial_candidate_published", {"commit": commit, "branch": config.skill_repository.branch, "candidate": state.get("candidate")}, iteration=0)
            self.store.update(task_id, current_iteration=0, status="ready")
            next_state = self._transition(
                task_id,
                "created",
                iteration=0,
                champion_commit=commit,
                challenger_commit=None,
                candidate_commit=commit,
                candidate_status="promoted",
                active_batch=None,
                brain_stage=None,
                environment_contract=None,
                environment_contract_hash=None,
                decision=None,
                approved_decision=None,
                candidate=None,
            )
            return {"commit": commit, "iteration": 0, "state": next_state, "initial_build": True}
        next_iteration = int(state.get("iteration", 0)) + 1
        self.store.append_event(task_id, "candidate.published", {"commit": commit, "branch": config.skill_repository.branch, "candidate": state.get("candidate")}, iteration=next_iteration)
        self.store.update(task_id, current_iteration=next_iteration, status="ready")
        next_state = self._transition(
            task_id,
            "evaluation_ready",
            iteration=next_iteration,
            challenger_commit=commit,
            candidate_commit=commit,
            candidate_status="evaluating",
            active_batch=None,
            brain_stage=None,
            environment_contract=None,
            environment_contract_hash=None,
            decision=None,
            approved_decision=None,
        )
        return {"commit": commit, "iteration": next_iteration, "state": next_state}

    def advance(self, task_id: str, *, wait: bool = False) -> Mapping[str, Any]:
        """Perform one automatic step and stop at user/external mutation gates."""

        phase = str(self.state(task_id).get("phase"))
        if phase == "created":
            state = self.state(task_id)
            if state.get("intent_mode") in ("extend", "discover", "create") and not state.get("blueprint_approved"):
                return self.plan_capabilities(task_id)
            return self.compile_design(task_id)
        if phase == "ready_to_build":
            return self.build_initial_candidate(task_id)
        if phase == "design_ready":
            return {"state": self.state(task_id), "gate": "design_ready"}
        if phase == "evaluation_ready":
            state = self.state(task_id)
            baseline_path = self._coordinator(task_id).batch_path(task_id, int(state.get("iteration", 0)), "without-skill-baseline")
            if state.get("intent_mode") == "create" and int(state.get("iteration", 0)) == 0 and not baseline_path.is_file():
                return self.dispatch(task_id, purpose="without-skill-baseline")
            return self.dispatch(task_id)
        if phase in ("remote_running", "verification_running", "baseline_running"):
            return self.collect(task_id, wait=wait)
        if phase == "baseline_collected":
            self._transition(task_id, "evaluation_ready", active_batch=None)
            return self.dispatch(task_id)
        if phase in ("remote_collected", "verification_collected", "evidence_ready", "semantic_grading", "attribution", "proposal"):
            decision = self.analyze(task_id)
            if decision.get("next_action") == "verify_passes":
                return self.dispatch(task_id, purpose="pass-verification", case_ids=tuple(decision["verification_required_case_ids"]))
            return decision
        if phase == "ready_to_optimize":
            return self.optimize(task_id)
        if phase == "candidate_ready":
            return self.publish_candidate(task_id)
        return {"state": self.state(task_id), "gate": phase}

    def run_until_gate(self, task_id: str, *, max_steps: int = 200) -> Mapping[str, Any]:
        """Power the desktop's one-button run while preserving explicit gates."""

        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps <= 0:
            raise IterationKernelError("max_steps must be a positive integer")
        gates = {"blueprint_ready", "discovery_ready", "initial_candidate_ready", "design_ready", "awaiting_confirmation", "needs_evidence", "blocked", "converged"}
        for _ in range(max_steps):
            phase = str(self.state(task_id).get("phase"))
            if phase in gates:
                return {"gate": phase, "snapshot": self.snapshot(task_id)}
            self.advance(task_id, wait=True)
        raise IterationKernelError("automatic run exceeded its state-transition safety limit")


__all__ = ["IterationKernel", "IterationKernelError", "KERNEL_STATE_API_VERSION"]
