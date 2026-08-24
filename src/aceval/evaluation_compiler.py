"""User-facing evaluation compilation, reuse, and conservative auto-generation.

The compiler keeps EvalPack mechanics behind the Doctor workflow.  It derives
an internal evaluation profile from the Skill and acceptance standards, then
uses an exact semantic signature to reuse a trusted EvalSuite.  When no exact
suite exists it generates the smallest safe draft and reports only the missing
acceptance evidence that a user must supply.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePath
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from .contracts import FrozenEvalPack, as_primitive
from .experiments import (
    ExperimentPlan,
    adjacent_experiment_path,
    policy_from_mapping,
    write_experiment_plan,
)
from .pack import EvalPackLoader, PackError
from .pack_builder import generate_evalpack, infer_objective_from_goal
from .pack_lifecycle import pack_calibration_status
from .planning_workflow import load_seed_cases
from .registry import build_builtin_registry
from .skill_analysis import CapabilityGraph, analyze_skill


EVALUATION_COMPILER_API_VERSION = "aceval.evaluation-compiler/v1"


class EvaluationCompilerError(ValueError):
    """The requested evaluation cannot be compiled without guessing."""


@dataclass(frozen=True)
class EvaluationProfile:
    id: str
    outcome_contract: str
    execution_shape: str
    oracle_strategy: str
    risk_level: str
    dimensions: Tuple[str, ...]
    runtime_requirements: Tuple[str, ...]
    matched_signals: Tuple[str, ...] = ()

    def to_dict(self, *, include_signals: bool = True) -> Mapping[str, Any]:
        value = {
            "id": self.id,
            "outcome_contract": self.outcome_contract,
            "execution_shape": self.execution_shape,
            "oracle_strategy": self.oracle_strategy,
            "risk_level": self.risk_level,
            "dimensions": list(self.dimensions),
            "runtime_requirements": list(self.runtime_requirements),
        }
        if include_signals:
            value["matched_signals"] = list(self.matched_signals)
        return value


@dataclass(frozen=True)
class EvaluationCompilation:
    pack: FrozenEvalPack
    experiment_path: Path
    profile: EvaluationProfile
    signature: str
    source: str
    lifecycle: str
    required_inputs: Tuple[Mapping[str, str], ...] = ()
    runtime_gaps: Tuple[str, ...] = ()

    @property
    def reusable(self) -> bool:
        return self.lifecycle in ("frozen", "legacy")

    @property
    def status(self) -> str:
        if self.required_inputs:
            return "needs_user_input"
        if self.runtime_gaps:
            return "runtime_adapter_required"
        if not self.reusable:
            return "evaluation_review_required"
        return "ready"

    def summary(self) -> Mapping[str, Any]:
        return {
            "api_version": EVALUATION_COMPILER_API_VERSION,
            "status": self.status,
            "source": self.source,
            "reused": self.source == "reused",
            "profile": self.profile.to_dict(),
            "signature": self.signature,
            "lifecycle": self.lifecycle,
            "required_inputs": [dict(item) for item in self.required_inputs],
            "runtime_gaps": list(self.runtime_gaps),
            "artifacts": {
                "evaluation_suite": str(self.pack.root),
                "experiment_plan": str(self.experiment_path),
            },
        }


_PROFILE_RULES = (
    (
        "design-to-code",
        ("d2c", "design to code", "design-to-code", "设计稿", "figma", "ui 还原", "页面还原"),
        "rendered_artifact",
        "artifact_generation",
        "visual_and_structural",
        "high",
        ("visual_fidelity", "interaction", "responsive_layout", "code_quality"),
        ("browser", "screenshot", "workspace_fixture", "artifact_output"),
    ),
    (
        "code-review",
        ("code review", "code-review", "代码评审", "代码审查", "前端 cr", "reviewer"),
        "grounded_findings",
        "repository_analysis",
        "record_and_source_match",
        "medium",
        ("precision", "recall", "source_grounding", "severity", "no_false_positive"),
        ("workspace_fixture", "canonical_trace"),
    ),
    (
        "online-document-edit",
        ("citadel", "学城", "在线 md", "在线md", "markdown 编辑", "文档编辑", "文档权限", "评论"),
        "remote_state_delta",
        "stateful_remote_edit",
        "before_after_and_audit",
        "high",
        ("content_accuracy", "format_preservation", "permission", "idempotency", "side_effect_scope"),
        ("network", "authentication", "remote_state_snapshot", "canonical_trace"),
    ),
    (
        "data-query",
        ("sql", "hive", "doris", "bi ", "取数", "数据查询", "dashboard", "仪表板", "数据集"),
        "structured_dataset",
        "read_only_remote_query",
        "result_and_query_policy",
        "high",
        ("result_accuracy", "query_safety", "scope", "freshness", "cost"),
        ("network", "authentication", "query_sandbox", "canonical_trace"),
    ),
    (
        "tabular-file-transform",
        ("csv", "tabular", "表格文件", "汇总表", "summary.json"),
        "structured_artifact",
        "local_artifact_transform",
        "exact_result_and_artifact_diff",
        "medium",
        ("content_accuracy", "schema", "input_preservation", "artifact_integrity"),
        ("workspace_fixture", "artifact_output", "canonical_trace"),
    ),
    (
        "structured-document",
        ("xlsx", "excel", "docx", "word", "pptx", "电子表格", "演示文稿", "多维表格"),
        "document_artifact",
        "artifact_transform",
        "structural_and_rendered_diff",
        "medium",
        ("content_accuracy", "structure", "format_preservation", "formula_or_layout", "artifact_integrity"),
        ("binary_artifact", "artifact_output", "render_preview"),
    ),
    (
        "observability-diagnosis",
        ("raptor", "logan", "alert", "alarm", "incident", "告警", "监控", "日志分析", "根因"),
        "evidence_backed_diagnosis",
        "multi_source_read_only_analysis",
        "grounded_diagnosis",
        "high",
        ("root_cause_accuracy", "evidence_grounding", "time_range", "tool_efficiency", "no_unsafe_action"),
        ("network", "authentication", "observability_tools", "canonical_trace"),
    ),
    (
        "workflow-transaction",
        ("approval", "审批", "工单", "ones", "devops", "发布平台", "流程标准"),
        "workflow_state_delta",
        "stateful_remote_transaction",
        "before_after_and_audit",
        "critical",
        ("target_accuracy", "permission", "state_transition", "idempotency", "rollback"),
        ("network", "authentication", "remote_state_snapshot", "approval_gate"),
    ),
    (
        "authentication-dependency",
        ("sso", "鉴权", "认证", "登录", "token", "占位符"),
        "credential_capability",
        "dependency_and_policy",
        "contract_and_negative_paths",
        "critical",
        ("credential_isolation", "permission_scope", "expiry", "negative_path", "secret_redaction"),
        ("authentication", "secret_injection", "canonical_trace"),
    ),
)

_DEFAULT_PROFILE = EvaluationProfile(
    id="generic-work-product",
    outcome_contract="structured_result",
    execution_shape="single_session",
    oracle_strategy="expected_result",
    risk_level="medium",
    dimensions=("correctness", "completeness", "format", "safety"),
    runtime_requirements=("fresh_session", "canonical_trace"),
)

_REFERENCE_CAPABILITIES = frozenset(
    ("fresh_session", "canonical_trace", "workspace_fixture", "artifact_output")
)


def classify_evaluation(
    subject: Union[str, Path, Any],
    standards: str,
) -> Tuple[EvaluationProfile, CapabilityGraph]:
    graph = analyze_skill(subject)
    corpus = json.dumps(
        graph.to_dict(), ensure_ascii=False, sort_keys=True
    ).casefold() + "\n" + str(standards).casefold()
    matches = []
    for priority, rule in enumerate(_PROFILE_RULES):
        keywords = rule[1]
        found = tuple(keyword for keyword in keywords if keyword.casefold() in corpus)
        if found:
            matches.append((len(found), -priority, rule, found))
    if not matches:
        return _DEFAULT_PROFILE, graph
    _, _, rule, found = max(matches, key=lambda item: (item[0], item[1]))
    return (
        EvaluationProfile(
            id=rule[0],
            outcome_contract=rule[2],
            execution_shape=rule[3],
            oracle_strategy=rule[4],
            risk_level=rule[5],
            dimensions=tuple(rule[6]),
            runtime_requirements=tuple(rule[7]),
            matched_signals=tuple(found),
        ),
        graph,
    )


def compile_evaluation(
    subject: Union[str, Path, Any],
    goal: str,
    output_dir: Union[str, Path],
    *,
    standards: Optional[str] = None,
    cases: Optional[Union[str, Path, Mapping[str, Any], Sequence[Mapping[str, Any]]]] = None,
    prompt: Optional[str] = None,
    expected_output: Any = None,
    has_expected_output: bool = False,
    pack_type: Optional[str] = None,
    objective: Optional[Mapping[str, Any]] = None,
    source_root: Optional[Union[str, Path]] = None,
    reuse_roots: Sequence[Union[str, Path]] = (),
    reuse: bool = True,
    test_design: Optional[Mapping[str, Any]] = None,
    allow_output_variant: bool = False,
) -> EvaluationCompilation:
    normalized_goal = _required_text(goal, "goal")
    normalized_standards = _required_text(
        standards if standards is not None else normalized_goal,
        "standards",
    )
    if cases is not None and (prompt is not None or has_expected_output):
        raise EvaluationCompilerError(
            "cases cannot be combined with prompt or expected_output"
        )
    profile, graph = classify_evaluation(subject, normalized_standards)
    cases_document, cases_source = _cases_document(
        cases,
        profile,
        normalized_standards,
        prompt,
        expected_output,
        has_expected_output,
    )
    fixture_root = (
        Path(source_root).expanduser().resolve()
        if source_root is not None
        else (cases_source.parent if cases_source is not None else Path.cwd().resolve())
    )
    selected_type = _select_pack_type(pack_type, profile, cases_document)
    signature = _evaluation_signature(
        profile,
        normalized_standards,
        cases_document,
        fixture_root,
        selected_type,
    )
    required_inputs = _required_inputs(cases_document)
    runtime_gaps = tuple(
        item
        for item in profile.runtime_requirements
        if item not in _REFERENCE_CAPABILITIES
    )
    registry = build_builtin_registry()
    reusable = (
        _find_reusable_pack(signature, reuse_roots, registry, graph.subject_hash)
        if reuse
        else None
    )
    if reusable is not None:
        pack, lifecycle = reusable
        output_parent = Path(output_dir).expanduser().resolve(strict=False).parent
        experiment_root = (
            output_parent.parent / "experiments"
            if output_parent.name == "packs"
            else output_parent / "experiments"
        )
        experiment_path = _experiment_for_reused_suite(
            pack,
            profile,
            signature,
            normalized_goal,
            objective,
            experiment_root,
        )
        return EvaluationCompilation(
            pack=pack,
            experiment_path=experiment_path,
            profile=profile,
            signature=signature,
            source="reused",
            lifecycle=lifecycle,
            required_inputs=required_inputs,
            runtime_gaps=runtime_gaps,
        )

    output = Path(output_dir).expanduser().resolve(strict=False)
    if (output.exists() or output.is_symlink()) and allow_output_variant:
        output = output.parent / (
            "%s-%s" % (output.name, signature[7:19])
        )
    evaluation_profile = dict(profile.to_dict())
    evaluation_profile.update(
        {
            "api_version": EVALUATION_COMPILER_API_VERSION,
            "signature": signature,
            "standards_hash": _canonical_hash(normalized_standards),
            "case_source": "provided" if cases is not None else "generated",
            "runtime_support": (
                "reference"
                if set(profile.runtime_requirements).issubset(_REFERENCE_CAPABILITIES)
                else "adapter_required"
            ),
        }
    )
    built = generate_evalpack(
        cases_document,
        selected_type,
        normalized_goal,
        output,
        objective=objective,
        source_root=fixture_root,
        evaluation_profile=evaluation_profile,
        test_design=test_design,
    )
    pack = EvalPackLoader(registry).load(built.root)
    return EvaluationCompilation(
        pack=pack,
        experiment_path=built.experiment_path,
        profile=profile,
        signature=signature,
        source="generated",
        lifecycle=pack_calibration_status(pack),
        required_inputs=required_inputs,
        runtime_gaps=runtime_gaps,
    )


def default_output_path(
    subject: Union[str, Path, Any],
    goal: str,
    *,
    standards: Optional[str] = None,
    root: Union[str, Path] = ".aceval/packs",
) -> Path:
    profile, graph = classify_evaluation(subject, standards or goal)
    digest = _canonical_hash(
        {
            "profile": profile.id,
            "subject_hash": graph.subject_hash,
            "standards": standards or goal,
        }
    )[7:19]
    return Path(root).expanduser().resolve(strict=False) / (
        "%s-%s" % (profile.id, digest)
    )


def profile_catalog() -> Tuple[Mapping[str, Any], ...]:
    profiles = []
    for rule in _PROFILE_RULES:
        profiles.append(
            EvaluationProfile(
                id=rule[0],
                outcome_contract=rule[2],
                execution_shape=rule[3],
                oracle_strategy=rule[4],
                risk_level=rule[5],
                dimensions=tuple(rule[6]),
                runtime_requirements=tuple(rule[7]),
            ).to_dict(include_signals=False)
        )
    profiles.append(_DEFAULT_PROFILE.to_dict(include_signals=False))
    return tuple(profiles)


def _cases_document(
    cases: Optional[Union[str, Path, Mapping[str, Any], Sequence[Mapping[str, Any]]]],
    profile: EvaluationProfile,
    standards: str,
    prompt: Optional[str],
    expected_output: Any,
    has_expected_output: bool,
) -> Tuple[Mapping[str, Any], Optional[Path]]:
    if cases is not None:
        document, _, source = load_seed_cases(cases)
        return dict(document), source
    task_prompt = (
        _required_text(prompt, "prompt")
        if prompt is not None
        else (
            "Execute the Skill's primary capability and satisfy these acceptance "
            "standards:\n%s" % standards
        )
    )
    case = {
        "id": "auto-contract-dev",
        "prompt": task_prompt,
        "split": "dev",
        "tags": ["aceval-auto", profile.id],
        "metadata": {
            "aceval_compiler": {
                "profile_id": profile.id,
                "oracle_source": (
                    "user_expected_output" if has_expected_output else "pending"
                ),
            }
        },
    }
    if has_expected_output:
        case["expected_output"] = as_primitive(expected_output)
    return (
        {
            "name": "%s-auto" % profile.id,
            "version": "0.1.0",
            "description": "Evaluation draft compiled from user acceptance standards.",
            "cases": [case],
        },
        None,
    )


def _select_pack_type(
    override: Optional[str],
    profile: EvaluationProfile,
    document: Mapping[str, Any],
) -> str:
    if override and override != "auto":
        return str(override)
    values = document.get("cases", ())
    cases = tuple(item for item in values if isinstance(item, Mapping))
    if (
        profile.id == "code-review"
        and cases
        and all(item.get("fixtures") and "expected_records" in item for item in cases)
    ):
        return "security-review"
    if (
        profile.id in ("structured-document", "tabular-file-transform")
        and cases
        and all(item.get("fixtures") and "expected_output" in item for item in cases)
        and any(
            str(fixture if isinstance(fixture, str) else fixture.get("path", ""))
            .casefold()
            .endswith(".csv")
            for item in cases
            for fixture in item.get("fixtures", ())
        )
    ):
        return "csv-summary"
    return "generic"


def _required_inputs(document: Mapping[str, Any]) -> Tuple[Mapping[str, str], ...]:
    cases = document.get("cases", ())
    missing = []
    for item in cases if isinstance(cases, Sequence) else ():
        if not isinstance(item, Mapping):
            continue
        if not any(
            key in item
            for key in ("expected_output", "expected_records", "forbidden_records")
        ):
            missing.append(str(item.get("id", "unknown")))
    if not missing:
        return ()
    return (
        {
            "field": "expected_result",
            "message": (
                "Provide the expected result for: %s. You may instead supply a "
                "Cases file with case-specific expectations." % ", ".join(missing)
            ),
        },
    )


def _evaluation_signature(
    profile: EvaluationProfile,
    standards: str,
    document: Mapping[str, Any],
    fixture_root: Path,
    pack_type: str,
) -> str:
    return _canonical_hash(
        {
            "compiler": EVALUATION_COMPILER_API_VERSION,
            "profile": profile.to_dict(include_signals=False),
            "standards": standards,
            "pack_type": pack_type,
            "cases": as_primitive(document),
            "fixture_digests": _fixture_digests(document, fixture_root),
        }
    )


def _fixture_digests(
    document: Mapping[str, Any], fixture_root: Path
) -> Mapping[str, str]:
    result = {}
    cases = document.get("cases", ())
    for item in cases if isinstance(cases, Sequence) else ():
        if not isinstance(item, Mapping):
            continue
        for fixture in item.get("fixtures", ()):
            if not isinstance(fixture, str):
                continue
            pure = PurePath(fixture)
            if pure.is_absolute() or ".." in pure.parts:
                raise EvaluationCompilerError("fixture path must be safe and relative")
            path = (fixture_root / pure).resolve()
            try:
                path.relative_to(fixture_root.resolve())
            except ValueError as exc:
                raise EvaluationCompilerError("fixture path escapes source root") from exc
            if not path.is_file() or path.is_symlink():
                raise EvaluationCompilerError("fixture is not a regular file: %s" % fixture)
            result[fixture] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return {key: result[key] for key in sorted(result)}


def _find_reusable_pack(
    signature: str,
    roots: Sequence[Union[str, Path]],
    registry: Any,
    subject_hash: str,
) -> Optional[Tuple[FrozenEvalPack, str]]:
    loader = EvalPackLoader(registry)
    candidates = []
    seen = set()
    for root_value in roots:
        root = Path(root_value).expanduser().resolve(strict=False)
        manifests = [root / "pack.yaml"] if (root / "pack.yaml").is_file() else (
            sorted(root.rglob("pack.yaml")) if root.is_dir() and not root.is_symlink() else []
        )
        for manifest in manifests:
            pack_root = manifest.parent
            if pack_root in seen or pack_root.is_symlink():
                continue
            seen.add(pack_root)
            try:
                pack = loader.load(pack_root)
                profile = pack.manifest.metadata.extra.get("evaluation_profile")
                if not isinstance(profile, Mapping) or profile.get("signature") != signature:
                    continue
                design = pack.manifest.metadata.extra.get("test_design")
                if (
                    isinstance(design, Mapping)
                    and design.get("source_subject_hash") not in (None, subject_hash)
                ):
                    continue
                lifecycle = pack_calibration_status(pack)
            except (OSError, PackError, ValueError):
                continue
            priority = 0 if lifecycle in ("frozen", "legacy") else 1
            candidates.append((priority, str(pack.root), pack, lifecycle))
    if not candidates:
        return None
    _, _, pack, lifecycle = min(candidates, key=lambda item: (item[0], item[1]))
    return pack, lifecycle


def _experiment_for_reused_suite(
    pack: FrozenEvalPack,
    profile: EvaluationProfile,
    signature: str,
    goal: str,
    objective: Optional[Mapping[str, Any]],
    output_root: Path,
) -> Path:
    objective_value = dict(objective) if objective is not None else infer_objective_from_goal(goal)
    optimization = {
        "adapter": "skill_markdown_v1",
        "patchable_components": ["skill_instruction"],
        "allowed_paths": ["SKILL.md"],
        "visible_splits": ["dev"],
        "beam_width": 1,
        "max_rounds": 2,
        "max_candidate_snapshots": 2,
        "max_added_lines": 30,
        "forbid_case_literals": True,
        "mode": "auto",
        "goal": goal,
    }
    if objective_value is not None:
        optimization["objective"] = objective_value
    plan = ExperimentPlan(
        name="%s-optimization" % profile.id,
        suite_hash=pack.suite_hash,
        optimization=policy_from_mapping(optimization),
        source="evaluation_compiler",
        metadata={"evaluation_signature": signature},
    )
    adjacent = adjacent_experiment_path(pack.root)
    if adjacent.is_file():
        try:
            existing = ExperimentPlan.load(adjacent)
            if existing.to_dict() == plan.to_dict():
                return adjacent
        except ValueError:
            pass
    target = output_root / (plan.content_hash + ".experiment.json")
    if target.is_file():
        existing = ExperimentPlan.load(target)
        if existing.to_dict() != plan.to_dict():
            raise EvaluationCompilerError("ExperimentPlan hash collision")
        return target
    return write_experiment_plan(target, plan)


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        as_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationCompilerError("%s must be a non-empty string" % label)
    return value.strip()


__all__ = [
    "EVALUATION_COMPILER_API_VERSION",
    "EvaluationCompilerError",
    "EvaluationProfile",
    "EvaluationCompilation",
    "classify_evaluation",
    "compile_evaluation",
    "default_output_path",
    "profile_catalog",
]
