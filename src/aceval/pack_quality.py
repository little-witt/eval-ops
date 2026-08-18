"""Quality gates for generated test-design sidecars and EvalPacks."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union

from .contracts import FrozenEvalPack, as_primitive
from .pack import EvalPackLoader
from .registry import build_builtin_registry


PACK_QUALITY_API_VERSION = "aceval.pack-quality/v1"
TEST_DESIGN_API_VERSION = "aceval.test-design/v1"
_SIDECAR_API_VERSIONS = {
    "capability_graph": "aceval.skill-analysis/v1",
    "test_plan": "aceval.test-plan/v1",
    "coverage_target": "aceval.coverage-report/v1",
    "generation_provenance": "aceval.case-generation/v1",
}
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

TRUSTED_HARD_ORACLES = frozenset(
    ("deterministic", "seed_derived", "reference_differential", "human_confirmed")
)
GENERATED_CASE_ORIGINS = frozenset(
    (
        "generated",
        "generated_boundary",
        "generated_negative",
        "requirement_synthesis",
        "metamorphic",
        "seed_expansion",
    )
)


class PackQualityError(ValueError):
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


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    severity: str
    ref: str = ""

    def to_dict(self) -> Mapping[str, Any]:
        return as_primitive(self)


@dataclass(frozen=True)
class PackQualityReport:
    pack_name: str
    pack_hash: str
    test_design_present: bool
    requirement_count: int
    mapped_requirement_count: int
    critical_requirement_count: int
    critical_mapped_count: int
    executable_requirement_count: int
    oracle_ready_case_count: int
    case_count: int
    issues: Tuple[QualityIssue, ...]
    mutation_score: Optional[float] = None
    api_version: str = PACK_QUALITY_API_VERSION

    @property
    def blockers(self) -> Tuple[QualityIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "blocker")

    @property
    def warnings(self) -> Tuple[QualityIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "warning")

    @property
    def ready_for_freeze(self) -> bool:
        return not self.blockers

    @property
    def planned_coverage(self) -> Optional[float]:
        if not self.requirement_count:
            return None
        return self.mapped_requirement_count / self.requirement_count

    @property
    def critical_coverage(self) -> Optional[float]:
        if not self.critical_requirement_count:
            return None
        return self.critical_mapped_count / self.critical_requirement_count

    @property
    def oracle_ready_coverage(self) -> Optional[float]:
        if not self.case_count:
            return None
        return self.oracle_ready_case_count / self.case_count

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "pack_name": self.pack_name,
            "pack_hash": self.pack_hash,
            "test_design_present": self.test_design_present,
            "ready_for_freeze": self.ready_for_freeze,
            "requirement_count": self.requirement_count,
            "mapped_requirement_count": self.mapped_requirement_count,
            "critical_requirement_count": self.critical_requirement_count,
            "critical_mapped_count": self.critical_mapped_count,
            "executable_requirement_count": self.executable_requirement_count,
            "case_count": self.case_count,
            "oracle_ready_case_count": self.oracle_ready_case_count,
            "planned_coverage": self.planned_coverage,
            "critical_coverage": self.critical_coverage,
            "oracle_ready_coverage": self.oracle_ready_coverage,
            "mutation_score": self.mutation_score,
            "blockers": [item.to_dict() for item in self.blockers],
            "warnings": [item.to_dict() for item in self.warnings],
            "issues": [item.to_dict() for item in self.issues],
        }


def _safe_ref(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PackQualityError("%s must be a non-empty relative path" % label)
    text = value.strip()
    pure = PurePath(text)
    windows = PureWindowsPath(text)
    if (
        pure.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in pure.parts
        or ".." in windows.parts
    ):
        raise PackQualityError("%s must stay inside the Pack" % label)
    path = root.joinpath(*pure.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise PackQualityError("%s does not resolve inside the Pack" % label) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise PackQualityError("%s must reference a regular file" % label)
    return resolved


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PackQualityError("cannot read %s: %s" % (label, exc)) from exc
    if not isinstance(value, Mapping):
        raise PackQualityError("%s must contain a JSON object" % label)
    return value


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _entries(value: Mapping[str, Any], *keys: str) -> Tuple[Mapping[str, Any], ...]:
    raw = None
    for key in keys:
        if key in value:
            raw = value[key]
            break
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        raw = tuple(raw.values())
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise PackQualityError("%s must be an array" % (keys[0] if keys else "entries"))
    result = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise PackQualityError("test-design entries must be objects")
        result.append(dict(item))
    return tuple(result)


def _requirement_id(value: Mapping[str, Any]) -> str:
    item = value.get("id", value.get("requirement_id"))
    return str(item or "").strip()


def _is_critical(value: Mapping[str, Any]) -> bool:
    return bool(value.get("critical")) or str(value.get("priority", value.get("risk", ""))).casefold() == "critical"


def _waived(value: Mapping[str, Any]) -> bool:
    waiver = value.get("waiver")
    if isinstance(waiver, Mapping):
        return bool(str(waiver.get("reason", "")).strip())
    return bool(waiver)


def _executable(value: Mapping[str, Any]) -> bool:
    if "executable" in value:
        return value.get("executable") is True
    status = str(value.get("status", value.get("feasibility", ""))).casefold()
    return status not in {"blocked_capability", "unsupported", "unobservable", "unconfigured", "unsafe"}


def _load_pack(value: Union[str, Path, FrozenEvalPack]) -> FrozenEvalPack:
    if isinstance(value, FrozenEvalPack):
        return value
    registry = build_builtin_registry()
    return EvalPackLoader(registry).load(value)


def evaluate_pack_quality(
    value: Union[str, Path, FrozenEvalPack],
) -> PackQualityReport:
    pack = _load_pack(value)
    test_design = pack.manifest.metadata.extra.get("test_design")
    if not isinstance(test_design, Mapping):
        return PackQualityReport(
            pack_name=pack.manifest.metadata.name,
            pack_hash=pack.pack_hash,
            test_design_present=False,
            requirement_count=0,
            mapped_requirement_count=0,
            critical_requirement_count=0,
            critical_mapped_count=0,
            executable_requirement_count=0,
            oracle_ready_case_count=0,
            case_count=len(pack.scenarios),
            issues=(),
        )

    issues = []
    if test_design.get("api_version") != TEST_DESIGN_API_VERSION:
        issues.append(
            QualityIssue(
                code="test_design.unsupported_version",
                message="test_design.api_version must be %s" % TEST_DESIGN_API_VERSION,
                severity="blocker",
                ref="manifest.metadata.test_design.api_version",
            )
        )

    documents = {}
    references = {
        "capability_graph": "capability_graph_ref",
        "test_plan": "test_plan_ref",
        "coverage_target": "coverage_target_ref",
        "generation_provenance": "generation_provenance_ref",
    }
    for name, key in references.items():
        try:
            path = _safe_ref(pack.root, test_design.get(key), "test_design.%s" % key)
            documents[name] = _read_json(path, key)
        except PackQualityError as exc:
            issues.append(
                QualityIssue(
                    code="test_design.invalid_ref",
                    message=str(exc),
                    severity="blocker",
                    ref="manifest.metadata.test_design.%s" % key,
                )
            )

    graph = documents.get("capability_graph", {})
    plan = documents.get("test_plan", {})
    coverage_target = documents.get("coverage_target", {})
    provenance = documents.get("generation_provenance", {})
    for name, expected_version in _SIDECAR_API_VERSIONS.items():
        document = documents.get(name)
        if document is not None and document.get("api_version") != expected_version:
            issues.append(
                QualityIssue(
                    "test_design.unsupported_sidecar_version",
                    "%s.api_version must be %s" % (name, expected_version),
                    "blocker",
                    "design/%s" % name.replace("_", "-"),
                )
            )

    subject_hash_fields = {
        "manifest.metadata.test_design.source_subject_hash": test_design.get(
            "source_subject_hash"
        ),
        "design/capability-graph.json": graph.get("subject_hash"),
        "design/test-plan.json": plan.get("subject_hash"),
        "design/coverage-target.json": coverage_target.get("subject_hash"),
        "design/generation-provenance.json": provenance.get("subject_hash"),
    }
    subject_hashes = set()
    for ref, subject_hash in subject_hash_fields.items():
        if not isinstance(subject_hash, str) or not _SHA256.fullmatch(subject_hash):
            issues.append(
                QualityIssue(
                    "test_design.invalid_subject_hash",
                    "%s must contain a sha256 Subject hash" % ref,
                    "blocker",
                    ref,
                )
            )
            continue
        subject_hashes.add(subject_hash)
    if len(subject_hashes) > 1:
        issues.append(
            QualityIssue(
                "test_design.subject_hash_mismatch",
                "test-design sidecars disagree on source Subject hash",
                "blocker",
                "manifest.metadata.test_design",
            )
        )
    expected_plan_hash = (
        provenance.get("test_plan_hash")
        if isinstance(provenance, Mapping)
        else None
    )
    if plan and expected_plan_hash != _canonical_hash(plan):
        issues.append(
            QualityIssue(
                "test_design.plan_hash_mismatch",
                "generation provenance does not match design/test-plan.json",
                "blocker",
                "design/generation-provenance.json",
            )
        )
    if plan:
        try:
            plan_blockers = _entries(plan, "freeze_blockers")
        except PackQualityError as exc:
            issues.append(
                QualityIssue(
                    "test_plan.invalid_blockers",
                    str(exc),
                    "blocker",
                    "design/test-plan",
                )
            )
            plan_blockers = ()
        for blocker in plan_blockers:
            code = str(blocker.get("code", "planning.freeze_blocker"))
            message = str(blocker.get("message", "planning freeze blocker"))
            ref = str(
                blocker.get("requirement_id")
                or blocker.get("capability_id")
                or "design/test-plan"
            )
            issues.append(
                QualityIssue(
                    code="planning.%s" % code,
                    message=message,
                    severity="blocker",
                    ref=ref,
                )
            )
    capabilities = _entries(graph, "capabilities") if graph else ()
    capability_ids = {str(item.get("id", "")) for item in capabilities if item.get("id")}
    try:
        requirements = _entries(plan, "requirements", "test_requirements") if plan else ()
    except PackQualityError as exc:
        issues.append(QualityIssue("test_plan.invalid", str(exc), "blocker", "design/test-plan"))
        requirements = ()
    runtime_gaps = _entries(plan, "runtime_gaps") if plan else ()
    gap_requirement_ids = {
        str(item.get("requirement_id", ""))
        for item in runtime_gaps
        if item.get("requirement_id")
    }

    requirement_by_id = {}
    for requirement in requirements:
        requirement_id = _requirement_id(requirement)
        if not requirement_id:
            issues.append(QualityIssue("requirement.missing_id", "test requirement is missing an id", "blocker", "design/test-plan"))
            continue
        if requirement_id in requirement_by_id:
            issues.append(QualityIssue("requirement.duplicate", "duplicate requirement id: %s" % requirement_id, "blocker", requirement_id))
        requirement_by_id[requirement_id] = requirement
        capability_id = str(requirement.get("capability_id", ""))
        if capability_id and capability_id not in capability_ids:
            issues.append(QualityIssue("requirement.unknown_capability", "requirement %s references unknown capability %s" % (requirement_id, capability_id), "blocker", requirement_id))

    case_requirements = {}
    family_splits = {}
    oracle_ready = 0
    for scenario in pack.scenarios:
        metadata = scenario.scenario.metadata.get("aceval_test", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        ids = metadata.get("requirement_ids", ())
        if isinstance(ids, str):
            ids = (ids,)
        if isinstance(ids, Sequence) and not isinstance(ids, (str, bytes)):
            ids = tuple(str(item) for item in ids)
        else:
            ids = ()
        case_requirements[scenario.id] = ids
        for requirement_id in ids:
            if requirement_id not in requirement_by_id:
                issues.append(QualityIssue("case.unknown_requirement", "case %s references unknown requirement %s" % (scenario.id, requirement_id), "blocker", scenario.id))
        family = str(metadata.get("family_id", "")).strip()
        if family:
            family_splits.setdefault(family, set()).add(scenario.split)
        origin = str(metadata.get("origin", "seed"))
        if scenario.split == "holdout" and origin in GENERATED_CASE_ORIGINS:
            issues.append(QualityIssue("case.generated_holdout", "generated case %s cannot be treated as sealed holdout" % scenario.id, "blocker", scenario.id))
        trust = str(metadata.get("oracle_trust", metadata.get("oracle_status", ""))).casefold()
        oracle_data = scenario.oracle.data if scenario.oracle is not None else None
        oracle_has_assertion = isinstance(oracle_data, Mapping) and not bool(
            oracle_data.get("calibration_required")
        ) and any(
            key in oracle_data
            for key in ("expected_output", "expected_records", "forbidden_records")
        )
        explicitly_pending = (
            metadata.get("oracle_ready") is False
            or metadata.get("needs_user_input") is True
        )
        if explicitly_pending:
            issues.append(
                QualityIssue(
                    "case.oracle_pending",
                    "case %s still requires Oracle calibration" % scenario.id,
                    "blocker",
                    scenario.id,
                )
            )
        elif trust in TRUSTED_HARD_ORACLES and oracle_has_assertion:
            oracle_ready += 1
        elif scenario.grader_ids:
            issues.append(QualityIssue("case.untrusted_oracle", "case %s hard Oracle is not confirmed" % scenario.id, "blocker", scenario.id))
        if metadata.get("executable") is False:
            issues.append(QualityIssue("case.runtime_gap", "case %s is not executable on the selected Runtime" % scenario.id, "blocker", scenario.id))

    for family, splits in family_splits.items():
        if len(splits) > 1:
            issues.append(QualityIssue("case.family_split_leakage", "case family %s spans splits: %s" % (family, ", ".join(sorted(splits))), "blocker", family))

    mapped_ids = {item for ids in case_requirements.values() for item in ids}
    critical = tuple(item for item in requirements if _is_critical(item))
    critical_ids = {_requirement_id(item) for item in critical}
    for requirement_id, requirement in requirement_by_id.items():
        if requirement_id in gap_requirement_ids:
            severity = "warning" if _waived(requirement) else (
                "blocker" if _is_critical(requirement) else "warning"
            )
            issues.append(QualityIssue("requirement.runtime_gap", "requirement %s is not executable on the selected Runtime" % requirement_id, severity, requirement_id))
        if requirement_id in mapped_ids:
            continue
        if _waived(requirement):
            issues.append(QualityIssue("requirement.waived", "requirement %s is uncovered with an explicit waiver" % requirement_id, "warning", requirement_id))
        elif _is_critical(requirement):
            issues.append(QualityIssue("requirement.critical_uncovered", "critical requirement %s has no Case" % requirement_id, "blocker", requirement_id))
        else:
            issues.append(QualityIssue("requirement.uncovered", "requirement %s has no Case" % requirement_id, "warning", requirement_id))
        if not _executable(requirement) and requirement_id not in gap_requirement_ids:
            issues.append(QualityIssue("requirement.runtime_gap", "requirement %s is not executable on the selected Runtime" % requirement_id, "warning" if _waived(requirement) else "blocker", requirement_id))

    mutation_score = provenance.get("mutation_score") if isinstance(provenance, Mapping) else None
    if mutation_score is not None:
        if isinstance(mutation_score, bool) or not isinstance(mutation_score, (int, float)) or not 0 <= float(mutation_score) <= 1:
            issues.append(QualityIssue("mutation.invalid_score", "mutation_score must be between 0 and 1", "blocker", "design/generation-provenance"))
            mutation_score = None
        else:
            mutation_score = float(mutation_score)

    return PackQualityReport(
        pack_name=pack.manifest.metadata.name,
        pack_hash=pack.pack_hash,
        test_design_present=True,
        requirement_count=len(requirement_by_id),
        mapped_requirement_count=len(mapped_ids.intersection(requirement_by_id)),
        critical_requirement_count=len(critical_ids),
        critical_mapped_count=len(critical_ids.intersection(mapped_ids)),
        executable_requirement_count=sum(
            _executable(item) and _requirement_id(item) not in gap_requirement_ids
            for item in requirements
        ),
        oracle_ready_case_count=oracle_ready,
        case_count=len(pack.scenarios),
        issues=tuple(issues),
        mutation_score=mutation_score,
    )


def validate_pack_quality_for_freeze(
    value: Union[str, Path, FrozenEvalPack]
) -> PackQualityReport:
    report = evaluate_pack_quality(value)
    if report.blockers:
        raise PackQualityError(
            "cannot freeze: Pack Quality blockers: %s"
            % "; ".join(item.message for item in report.blockers)
        )
    return report


__all__ = [
    "GENERATED_CASE_ORIGINS",
    "PACK_QUALITY_API_VERSION",
    "TEST_DESIGN_API_VERSION",
    "TRUSTED_HARD_ORACLES",
    "PackQualityError",
    "PackQualityReport",
    "QualityIssue",
    "evaluate_pack_quality",
    "validate_pack_quality_for_freeze",
]
