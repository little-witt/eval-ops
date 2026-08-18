"""Filesystem workflow for complex-Skill analysis and test-plan artifacts."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from .case_generation import CaseGenerationResult, compile_case_drafts
from .coverage import CoverageReport, build_coverage_report
from .skill_analysis import CapabilityGraph, analyze_skill
from .test_planning import TestPlan, plan_tests


PLANNING_WORKFLOW_API_VERSION = "aceval.planning-workflow/v1"
REFERENCE_RUNTIME_CAPABILITIES = frozenset(
    (
        "headless",
        "fresh_session",
        "workspace_fixture",
        "artifact_output",
        "canonical_trace",
        "fixed_parameters",
        "skill_activation",
    )
)


class PlanningWorkflowError(ValueError):
    pass


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant: %s" % value)


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PlanningWorkflowError("cannot read %s: %s" % (label, exc)) from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def load_seed_cases(
    value: Union[str, Path, Mapping[str, Any], Sequence[Mapping[str, Any]]]
) -> Tuple[Mapping[str, Any], Tuple[Mapping[str, Any], ...], Optional[Path]]:
    source = None
    if isinstance(value, (str, Path)):
        source = Path(value).expanduser().resolve()
        raw = _read_json(source, "seed cases")
    else:
        raw = value
    if isinstance(raw, Mapping):
        cases = raw.get("cases")
        document = dict(raw)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        cases = raw
        document = {"cases": list(raw)}
    else:
        raise PlanningWorkflowError("seed cases must be a JSON object or array")
    if isinstance(cases, (str, bytes)) or not isinstance(cases, Sequence) or not cases:
        raise PlanningWorkflowError("seed cases must contain a non-empty cases array")
    normalized = []
    for item in cases:
        if not isinstance(item, Mapping):
            raise PlanningWorkflowError("seed cases must contain objects")
        normalized.append(dict(item))
    return document, tuple(normalized), source


@dataclass(frozen=True)
class PlanningArtifacts:
    root: Path
    capability_graph: CapabilityGraph
    test_plan: TestPlan
    coverage: CoverageReport
    case_generation: CaseGenerationResult
    seed_cases_document: Mapping[str, Any]
    seed_source_root: Optional[Path] = None
    api_version: str = PLANNING_WORKFLOW_API_VERSION

    @property
    def outcome(self) -> str:
        return self.test_plan.outcome

    @property
    def freeze_blockers(self) -> Tuple[Any, ...]:
        return self.test_plan.freeze_blockers

    def test_design(self) -> Mapping[str, Any]:
        provenance = dict(self.case_generation.generation_provenance)
        provenance["test_plan_hash"] = _canonical_hash(self.test_plan.to_dict())
        return {
            "capability_graph": self.capability_graph.to_dict(),
            "test_plan": self.test_plan.to_dict(),
            "coverage_target": self.coverage.to_dict(),
            "generation_provenance": provenance,
        }

    def summary(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "root": str(self.root),
            "subject_hash": self.capability_graph.subject_hash,
            "outcome": self.outcome,
            "capability_count": len(self.capability_graph.capabilities),
            "requirement_count": len(self.test_plan.requirements),
            "case_count": len(self.test_plan.cases),
            "generated_case_count": len(
                self.case_generation.generated_case_ids
            ),
            "pending_oracle_case_count": len(
                self.case_generation.pending_oracle_case_ids
            ),
            "runtime_gap_count": len(self.test_plan.runtime_gaps),
            "freeze_blocker_count": len(self.test_plan.freeze_blockers),
            "coverage": {
                "planned": self.coverage.planned.to_dict(),
                "executable": self.coverage.executable.to_dict(),
                "oracle_ready": self.coverage.oracle_ready.to_dict(),
                "observed": self.coverage.observed.to_dict(),
            },
        }


def create_planning_artifacts(
    subject: Union[str, Path, Any],
    seed_cases: Union[str, Path, Mapping[str, Any], Sequence[Mapping[str, Any]]],
    goal: str,
    output_dir: Union[str, Path],
    *,
    runtime_capabilities: Optional[Sequence[str]] = tuple(
        sorted(REFERENCE_RUNTIME_CAPABILITIES)
    ),
    max_generated_cases: int = 12,
) -> PlanningArtifacts:
    output = Path(output_dir).expanduser().resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise PlanningWorkflowError(
            "planning output must not already exist: %s" % output
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    document, cases, source = load_seed_cases(seed_cases)
    graph = analyze_skill(subject)
    plan = plan_tests(
        graph,
        seed_cases=cases,
        goal=goal,
        runtime_capabilities=runtime_capabilities,
        max_generated_cases=max_generated_cases,
    )
    generated = compile_case_drafts(
        plan,
        cases,
        name=str(document.get("name", "planned-skill-eval")),
        version=str(document.get("version", "0.1.0")),
    )
    coverage = build_coverage_report(plan)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=".%s-plan-" % output.name,
            dir=str(output.parent),
        )
    )
    try:
        _write_json(temporary / "capability-graph.json", graph.to_dict())
        _write_json(temporary / "test-plan.json", plan.to_dict())
        _write_json(temporary / "coverage.json", coverage.to_dict())
        _write_json(temporary / "case-drafts.json", generated.to_dict())
        _write_json(
            temporary / "runtime-gaps.json",
            {
                "api_version": "aceval.runtime-gaps/v1",
                "runtime_profile_state": plan.runtime_profile_state,
                "runtime_capabilities": list(plan.runtime_capabilities),
                "runtime_gaps": [item.to_dict() for item in plan.runtime_gaps],
            },
        )
        _write_json(
            temporary / "generation-provenance.json",
            dict(generated.generation_provenance),
        )
        _write_json(temporary / "seed-cases.json", document)
        summary = PlanningArtifacts(
            root=output,
            capability_graph=graph,
            test_plan=plan,
            coverage=coverage,
            case_generation=generated,
            seed_cases_document=document,
            seed_source_root=source.parent if source is not None else None,
        ).summary()
        summary = dict(summary)
        summary["seed_cases_source"] = str(source) if source is not None else None
        _write_json(temporary / "summary.json", summary)
        temporary.rename(output)
    except Exception:
        if temporary.exists():
            shutil.rmtree(str(temporary))
        raise
    return PlanningArtifacts(
        root=output,
        capability_graph=graph,
        test_plan=plan,
        coverage=coverage,
        case_generation=generated,
        seed_cases_document=document,
        seed_source_root=source.parent if source is not None else None,
    )


def load_planning_artifacts(path: Union[str, Path]) -> PlanningArtifacts:
    root = Path(path).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise PlanningWorkflowError("planning artifact path must be a directory")
    graph = CapabilityGraph.from_dict(
        _read_json(root / "capability-graph.json", "capability graph")
    )
    plan = TestPlan.from_dict(_read_json(root / "test-plan.json", "test plan"))
    coverage = CoverageReport.from_dict(
        _read_json(root / "coverage.json", "coverage report")
    )
    expected_coverage = build_coverage_report(plan)
    if coverage.to_dict() != expected_coverage.to_dict():
        raise PlanningWorkflowError(
            "coverage.json is stale or inconsistent with test-plan.json"
        )
    generation_payload = _read_json(
        root / "case-drafts.json", "case drafts"
    )
    if not isinstance(generation_payload, Mapping):
        raise PlanningWorkflowError("case-drafts.json must contain an object")
    cases_document = generation_payload.get("cases_document")
    provenance = generation_payload.get("generation_provenance")
    if not isinstance(cases_document, Mapping) or not isinstance(provenance, Mapping):
        raise PlanningWorkflowError("case-drafts.json is missing generated case data")
    generated = CaseGenerationResult(
        api_version=str(generation_payload.get("api_version", "")),
        cases_document=dict(cases_document),
        generation_provenance=dict(provenance),
        active_case_ids=tuple(generation_payload.get("active_case_ids", ())),
        generated_case_ids=tuple(generation_payload.get("generated_case_ids", ())),
        pending_oracle_case_ids=tuple(
            generation_payload.get("pending_oracle_case_ids", ())
        ),
    )
    seed_document = _read_json(root / "seed-cases.json", "seed cases")
    if not isinstance(seed_document, Mapping):
        raise PlanningWorkflowError("seed-cases.json must contain an object")
    if graph.subject_hash != plan.subject_hash or plan.subject_hash != coverage.subject_hash:
        raise PlanningWorkflowError("planning artifacts disagree on subject_hash")
    summary_value = _read_json(root / "summary.json", "planning summary")
    seed_source_root = None
    if isinstance(summary_value, Mapping):
        seed_source = summary_value.get("seed_cases_source")
        if isinstance(seed_source, str) and seed_source:
            seed_source_root = Path(seed_source).expanduser().resolve().parent
    return PlanningArtifacts(
        root=root,
        capability_graph=graph,
        test_plan=plan,
        coverage=coverage,
        case_generation=generated,
        seed_cases_document=dict(seed_document),
        seed_source_root=seed_source_root,
    )


__all__ = [
    "PLANNING_WORKFLOW_API_VERSION",
    "REFERENCE_RUNTIME_CAPABILITIES",
    "PlanningArtifacts",
    "PlanningWorkflowError",
    "create_planning_artifacts",
    "load_planning_artifacts",
    "load_seed_cases",
]
