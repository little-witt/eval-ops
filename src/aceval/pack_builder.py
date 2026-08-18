"""Deterministic draft EvalPack generation and explicit calibration freeze.

The builder deliberately produces *drafts*.  A generated Pack is structurally
valid, but its generated evaluators are not treated as trusted until an
operator has reviewed the cases/oracles and explicitly freezes the Pack.

This module is intentionally independent from the CLI so it can also be used
by a future ``doctor`` workflow or a hosted Pack Builder service.
"""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePath, PureWindowsPath
import re
import shutil
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .contracts import as_primitive
from .pack import EvalPackLoader, LATEST_API_VERSION
from .pack_lifecycle import (
    CALIBRATION_CALIBRATING,
    CALIBRATION_DRAFT,
    CALIBRATION_FROZEN,
    CALIBRATION_STATUSES,
    PACK_LOCK_FILE,
    write_pack_lock,
)
from .pack_quality import (
    TEST_DESIGN_API_VERSION,
    PackQualityError,
    validate_pack_quality_for_freeze,
)
from .registry import build_builtin_registry


PACK_TYPES = frozenset(("generic", "csv-summary", "security-review"))
GENERATOR_ID = "aceval.pack-builder/v1"

_COMPONENT_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SPLITS = ("dev", "validation", "holdout")
_CASE_KEYS = frozenset(
    (
        "id",
        "prompt",
        "split",
        "fixtures",
        "expected_output",
        "expected_records",
        "forbidden_records",
        "timeout_seconds",
        "tags",
        "metadata",
    )
)
_DOCUMENT_KEYS = frozenset(("cases", "name", "version", "description"))


class PackBuilderError(ValueError):
    """The requested draft cannot be generated safely."""


class PackCalibrationError(PackBuilderError):
    """A draft is not ready to be frozen or lacks explicit approval."""


@dataclass(frozen=True)
class PackBuildResult:
    root: Path
    manifest_path: Path
    pack_type: str
    calibration_status: str
    pack_hash: str
    requested_type: str = ""

    @property
    def trusted(self) -> bool:
        """Only explicitly frozen Packs are eligible to be treated as trusted."""

        return self.calibration_status == CALIBRATION_FROZEN


@dataclass(frozen=True)
class _Case:
    id: str
    prompt: str
    split: str
    fixtures: Tuple[Any, ...]
    has_expected_output: bool
    expected_output: Any
    expected_records: Tuple[Mapping[str, Any], ...]
    forbidden_records: Tuple[Mapping[str, Any], ...]
    timeout_seconds: int
    tags: Tuple[str, ...]
    metadata: Mapping[str, Any]


CasesInput = Union[str, Path, Mapping[str, Any], Sequence[Mapping[str, Any]]]


def generate_evalpack(
    cases: CasesInput,
    pack_type: str,
    goal: str,
    output_dir: Union[str, Path],
    *,
    objective: Optional[Mapping[str, Any]] = None,
    name: Optional[str] = None,
    version: Optional[str] = None,
    description: Optional[str] = None,
    source_root: Optional[Union[str, Path]] = None,
    test_design: Optional[Mapping[str, Any]] = None,
    registry: Optional[Any] = None,
) -> PackBuildResult:
    """Generate a deterministic, structurally validated draft EvalPack.

    ``cases`` may be a JSON file, ``{"cases": [...]}``, or a case list.  File
    fixtures are resolved relative to the cases JSON file (or ``source_root``).
    Inline fixtures use ``{"path": "input.csv", "content": "..."}`` or
    ``content_base64``.

    The output path must not already exist.  This prevents an old evaluator or
    oracle from being silently retained in a newly generated Pack.
    """

    requested_type = _non_empty_string(pack_type, "pack_type")
    normalized_type = (
        requested_type if requested_type in PACK_TYPES else "generic"
    )
    normalized_goal = _non_empty_string(goal, "goal")
    document, inferred_root = _load_cases_document(cases)
    normalized_cases = _normalize_cases(document.get("cases"))
    if not any(item.split == "dev" for item in normalized_cases):
        raise PackBuilderError("at least one dev case is required")

    default_name = (
        "%s-generated" % requested_type
        if _COMPONENT_ID.match(requested_type)
        else "%s-generated" % normalized_type
    )
    pack_name = _component_id(
        name if name is not None else document.get("name", default_name),
        "name",
    )
    pack_version = _non_empty_string(
        version if version is not None else document.get("version", "0.1.0"),
        "version",
    )
    pack_description = str(
        description
        if description is not None
        else document.get(
            "description",
            "Generated %s EvalPack draft; evaluator calibration is required."
            % normalized_type,
        )
    ).strip()
    normalized_objective = _normalize_objective(objective)
    normalized_test_design = _normalize_test_design(test_design)
    objective_origin = "explicit" if normalized_objective is not None else "none"
    if normalized_objective is None:
        normalized_objective = infer_objective_from_goal(normalized_goal)
        if normalized_objective is not None:
            objective_origin = "goal_heuristic"

    fixture_root = (
        Path(source_root).expanduser().resolve()
        if source_root is not None
        else inferred_root
    )
    output = Path(output_dir).expanduser().resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise PackBuilderError("output_dir must not already exist: %s" % output)
    output.parent.mkdir(parents=True, exist_ok=True)

    temporary = Path(
        tempfile.mkdtemp(prefix=".%s-build-" % output.name, dir=str(output.parent))
    )
    try:
        _build_pack_tree(
            temporary,
            normalized_cases,
            normalized_type,
            normalized_goal,
            normalized_objective,
            pack_name,
            pack_version,
            pack_description,
            fixture_root,
            requested_type,
            objective_origin,
            normalized_test_design,
        )
        active_registry = registry if registry is not None else build_builtin_registry()
        EvalPackLoader(active_registry).load(temporary)
        temporary.rename(output)
        frozen = EvalPackLoader(active_registry).load(output)
    except Exception:
        if temporary.exists():
            shutil.rmtree(str(temporary))
        raise

    return PackBuildResult(
        root=output,
        manifest_path=output / "pack.yaml",
        pack_type=normalized_type,
        calibration_status=CALIBRATION_DRAFT,
        pack_hash=frozen.pack_hash,
        requested_type=requested_type,
    )


def freeze_evalpack(
    pack_dir: Union[str, Path],
    *,
    approve: bool = False,
    registry: Optional[Any] = None,
) -> PackBuildResult:
    """Validate and explicitly freeze a generated draft.

    Freezing changes only ``metadata.calibration_status``.  It never fills in
    missing oracles, changes graders, infers an objective, or rewrites cases.
    ``approve=True`` is intentionally required so callers cannot turn a draft
    into a trusted optimization gate as a side effect of loading it.
    """

    if approve is not True:
        raise PackCalibrationError("freezing requires explicit approve=True")
    root = Path(pack_dir).expanduser().resolve()
    manifest_path = root / "pack.yaml"
    original_bytes, document = _read_generated_manifest(manifest_path)
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        raise PackCalibrationError("pack.yaml metadata must be an object")
    status = metadata.get("calibration_status")
    if status not in CALIBRATION_STATUSES:
        raise PackCalibrationError(
            "metadata.calibration_status must be draft, calibrating, or frozen"
        )

    active_registry = registry if registry is not None else build_builtin_registry()
    loader = EvalPackLoader(active_registry)
    current_pack = loader.load(root)
    pack_type = _generated_pack_type(document)
    _validate_calibration_readiness(current_pack, pack_type)
    try:
        validate_pack_quality_for_freeze(current_pack)
    except PackQualityError as exc:
        raise PackCalibrationError(str(exc)) from exc
    if status == CALIBRATION_FROZEN:
        return PackBuildResult(
            root=root,
            manifest_path=manifest_path,
            pack_type=pack_type,
            calibration_status=CALIBRATION_FROZEN,
            pack_hash=current_pack.pack_hash,
            requested_type=_generated_requested_type(document, pack_type),
        )

    candidate = copy.deepcopy(document)
    candidate["metadata"]["calibration_status"] = CALIBRATION_FROZEN
    if _evaluation_semantics(document) != _evaluation_semantics(candidate):
        raise PackCalibrationError("freeze attempted to change evaluator semantics")

    candidate_bytes = _json_bytes(candidate)
    _atomic_write(manifest_path, candidate_bytes)
    try:
        write_pack_lock(root, candidate["metadata"].get("version"))
        frozen = loader.load(root)
    except Exception:
        lock_path = root / PACK_LOCK_FILE
        if lock_path.exists() and not lock_path.is_symlink():
            lock_path.unlink()
        _atomic_write(manifest_path, original_bytes)
        raise

    return PackBuildResult(
        root=root,
        manifest_path=manifest_path,
        pack_type=pack_type,
        calibration_status=CALIBRATION_FROZEN,
        pack_hash=frozen.pack_hash,
        requested_type=_generated_requested_type(candidate, pack_type),
    )


def calibration_status(pack_dir: Union[str, Path]) -> str:
    """Return the explicit lifecycle status without implying Pack trust."""

    _, document = _read_generated_manifest(Path(pack_dir) / "pack.yaml")
    metadata = document.get("metadata", {})
    status = metadata.get("calibration_status") if isinstance(metadata, Mapping) else None
    if status not in CALIBRATION_STATUSES:
        raise PackCalibrationError("Pack has no recognized calibration status")
    return str(status)


def begin_calibration(pack_dir: Union[str, Path]) -> PackBuildResult:
    """Mark a generated draft as actively being calibrated.

    This transition changes lifecycle metadata only.  Cases, Oracles, Graders,
    and objectives remain editable until ``freeze_evalpack`` creates the lock.
    """

    root = Path(pack_dir).expanduser().resolve()
    manifest_path = root / "pack.yaml"
    original_bytes, document = _read_generated_manifest(manifest_path)
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        raise PackCalibrationError("pack.yaml metadata must be an object")
    status = metadata.get("calibration_status")
    if status == CALIBRATION_FROZEN:
        raise PackCalibrationError(
            "frozen EvalPack cannot re-enter calibration; create a new Pack version"
        )
    if status not in (CALIBRATION_DRAFT, CALIBRATION_CALIBRATING):
        raise PackCalibrationError("Pack has no recognized calibration status")
    active_registry = build_builtin_registry()
    current = EvalPackLoader(active_registry).load(root)
    if status == CALIBRATION_CALIBRATING:
        return PackBuildResult(
            root=root,
            manifest_path=manifest_path,
            pack_type=_generated_pack_type(document),
            calibration_status=CALIBRATION_CALIBRATING,
            pack_hash=current.pack_hash,
            requested_type=_generated_requested_type(
                document, _generated_pack_type(document)
            ),
        )
    candidate = copy.deepcopy(document)
    candidate["metadata"]["calibration_status"] = CALIBRATION_CALIBRATING
    if _evaluation_semantics(document) != _evaluation_semantics(candidate):
        raise PackCalibrationError(
            "calibration transition attempted to change evaluator semantics"
        )
    _atomic_write(manifest_path, _json_bytes(candidate))
    try:
        loaded = EvalPackLoader(active_registry).load(root)
    except Exception:
        _atomic_write(manifest_path, original_bytes)
        raise
    pack_type = _generated_pack_type(candidate)
    return PackBuildResult(
        root=root,
        manifest_path=manifest_path,
        pack_type=pack_type,
        calibration_status=CALIBRATION_CALIBRATING,
        pack_hash=loaded.pack_hash,
        requested_type=_generated_requested_type(candidate, pack_type),
    )


def _load_cases_document(cases: CasesInput) -> Tuple[Dict[str, Any], Path]:
    inferred_root = Path.cwd().resolve()
    if isinstance(cases, (str, Path)):
        path = Path(cases).expanduser().resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PackBuilderError("cannot read cases JSON %s: %s" % (path, exc)) from exc
        inferred_root = path.parent
    else:
        raw = cases

    if isinstance(raw, Mapping):
        unknown = set(raw).difference(_DOCUMENT_KEYS)
        if unknown:
            raise PackBuilderError(
                "cases document contains unsupported field(s): %s"
                % ", ".join(sorted(str(item) for item in unknown))
            )
        document = dict(raw)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        document = {"cases": list(raw)}
    else:
        raise PackBuilderError("cases must be a JSON object or array")
    if "cases" not in document:
        raise PackBuilderError("cases document requires a cases array")
    return document, inferred_root


def _normalize_cases(value: Any) -> Tuple[_Case, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise PackBuilderError("cases must be a non-empty array")
    normalized = []  # type: List[_Case]
    seen = set()
    for index, item in enumerate(value):
        path = "cases[%d]" % index
        if not isinstance(item, Mapping):
            raise PackBuilderError("%s must be an object" % path)
        unknown = set(item).difference(_CASE_KEYS)
        if unknown:
            raise PackBuilderError(
                "%s contains unsupported field(s): %s"
                % (path, ", ".join(sorted(str(key) for key in unknown)))
            )
        case_id = _component_id(item.get("id"), path + ".id")
        if case_id in seen:
            raise PackBuilderError("duplicate case id: %s" % case_id)
        seen.add(case_id)
        split = str(item.get("split", "dev")).strip()
        if split not in _SPLITS:
            raise PackBuilderError("%s.split must be dev, validation, or holdout" % path)
        fixtures = item.get("fixtures", ())
        if not isinstance(fixtures, Sequence) or isinstance(fixtures, (str, bytes)):
            raise PackBuilderError("%s.fixtures must be an array" % path)
        expected_records = _record_list(item.get("expected_records", ()), path + ".expected_records")
        forbidden_records = _record_list(item.get("forbidden_records", ()), path + ".forbidden_records")
        timeout = item.get("timeout_seconds", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise PackBuilderError("%s.timeout_seconds must be a positive integer" % path)
        tags = item.get("tags", ())
        if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes)):
            raise PackBuilderError("%s.tags must be an array of strings" % path)
        if any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise PackBuilderError("%s.tags must contain non-empty strings" % path)
        metadata = item.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise PackBuilderError("%s.metadata must be an object" % path)
        normalized.append(
            _Case(
                id=case_id,
                prompt=_non_empty_string(item.get("prompt"), path + ".prompt"),
                split=split,
                fixtures=tuple(fixtures),
                has_expected_output="expected_output" in item,
                expected_output=item.get("expected_output"),
                expected_records=expected_records,
                forbidden_records=forbidden_records,
                timeout_seconds=timeout,
                tags=tuple(tag.strip() for tag in tags),
                metadata=dict(metadata),
            )
        )
    return tuple(normalized)


def _record_list(value: Any, path: str) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PackBuilderError("%s must be an array" % path)
    result = []
    for index, record in enumerate(value):
        if not isinstance(record, Mapping):
            raise PackBuilderError("%s[%d] must be an object" % (path, index))
        result.append(dict(record))
    return tuple(result)


def _normalize_objective(value: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    primitive = as_primitive(value)
    if not isinstance(primitive, Mapping):
        raise PackBuilderError("objective must be an object")
    # Serialization here rejects NaN/Infinity before the filesystem is changed.
    try:
        json.dumps(primitive, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PackBuilderError("objective must contain finite JSON values: %s" % exc) from exc
    return dict(primitive)


def _normalize_test_design(
    value: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PackBuilderError("test_design must be an object")
    required = {
        "capability_graph",
        "test_plan",
        "coverage_target",
        "generation_provenance",
    }
    missing = required.difference(value)
    unknown = set(value).difference(required)
    if missing:
        raise PackBuilderError(
            "test_design is missing field(s): %s"
            % ", ".join(sorted(missing))
        )
    if unknown:
        raise PackBuilderError(
            "test_design contains unsupported field(s): %s"
            % ", ".join(sorted(str(item) for item in unknown))
        )
    normalized = {}
    for key in sorted(required):
        item = as_primitive(value[key])
        if not isinstance(item, Mapping):
            raise PackBuilderError("test_design.%s must be an object" % key)
        normalized[key] = dict(item)
    plan_payload = json.dumps(
        normalized["test_plan"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    normalized["generation_provenance"].setdefault(
        "test_plan_hash", "sha256:" + hashlib.sha256(plan_payload).hexdigest()
    )
    try:
        json.dumps(normalized, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PackBuilderError(
            "test_design must contain finite JSON values: %s" % exc
        ) from exc
    return normalized


def infer_objective_from_goal(goal: str) -> Optional[Dict[str, Any]]:
    """Infer one conservative efficiency objective from a natural-language goal.

    Correctness and safety stay in hard Graders.  This helper intentionally
    recognizes only directly measurable efficiency language; subjective words
    such as "better" or "high quality" do not become a self-authored Judge.
    """

    text = _non_empty_string(goal, "goal").casefold()
    candidates = (
        (
            "tool-efficiency",
            {"type": "trace_count", "key": "tool_call"},
            ("tool call", "tool-call", "工具调用", "调用次数", "少调用"),
        ),
        (
            "cost-efficiency",
            {"type": "usage", "key": "cost_usd"},
            ("cost", "费用", "成本", "花费"),
        ),
        (
            "token-efficiency",
            {"type": "usage", "key": "total_tokens"},
            ("token", "tokens", "令牌", "上下文长度", "精简", "简洁"),
        ),
        (
            "latency",
            {"type": "scenario", "key": "duration_seconds"},
            ("latency", "延迟", "耗时", "响应时间", "更快", "速度"),
        ),
    )
    matched = []
    for priority, (objective_id, source, keywords) in enumerate(candidates):
        positions = [text.find(keyword) for keyword in keywords if keyword in text]
        if positions:
            matched.append((min(positions), priority, objective_id, source))
    if not matched:
        return None
    _, _, objective_id, source = min(matched)
    return {
        "id": objective_id,
        "source": source,
        "direction": "minimize",
        "aggregation": "mean",
        "min_delta": 0.0,
        "max_case_regression": 0.0,
    }


def _build_pack_tree(
    root: Path,
    cases: Sequence[_Case],
    pack_type: str,
    goal: str,
    objective: Optional[Mapping[str, Any]],
    name: str,
    version: str,
    description: str,
    source_root: Path,
    requested_type: str,
    objective_origin: str,
    test_design: Optional[Mapping[str, Any]],
) -> None:
    for directory in ("scenarios", "oracles", "fixtures", "schemas"):
        (root / directory).mkdir(parents=True, exist_ok=True)

    schema_path, schema = _schema_for(pack_type)
    _write_json(root / schema_path, schema)
    if test_design is not None:
        _write_json(
            root / "design" / "capability-graph.json",
            test_design["capability_graph"],
        )
        _write_json(
            root / "design" / "test-plan.json",
            test_design["test_plan"],
        )
        _write_json(
            root / "design" / "coverage-target.json",
            test_design["coverage_target"],
        )
        _write_json(
            root / "design" / "generation-provenance.json",
            test_design["generation_provenance"],
        )
    manifest = _manifest_for(
        cases,
        pack_type,
        goal,
        objective,
        name,
        version,
        description,
        schema_path,
        requested_type,
        objective_origin,
        test_design,
    )
    by_split = {split: [] for split in _SPLITS}  # type: Dict[str, List[Dict[str, Any]]]
    for case in cases:
        fixture_refs, workspace_paths = _materialize_fixtures(root, case, source_root)
        if pack_type in ("csv-summary", "security-review") and not fixture_refs:
            raise PackBuilderError("%s cases require at least one fixture" % pack_type)
        oracle = _oracle_for(case, pack_type)
        oracle_ref = "oracles/%s/%s.json" % (case.split, case.id)
        _write_json(root / oracle_ref, oracle)
        by_split[case.split].append(
            _scenario_for(case, pack_type, fixture_refs, workspace_paths, oracle_ref)
        )

    for split in _SPLITS:
        if by_split[split]:
            _write_json(
                root / "scenarios" / (split + ".yaml"),
                {"scenarios": by_split[split]},
            )
    _write_json(root / "pack.yaml", manifest)


def _manifest_for(
    cases: Sequence[_Case],
    pack_type: str,
    goal: str,
    objective: Optional[Mapping[str, Any]],
    name: str,
    version: str,
    description: str,
    schema_path: str,
    requested_type: str,
    objective_origin: str,
    test_design: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    suites = {item.split for item in cases}
    suite = {"dev": "scenarios/dev.yaml"}  # type: Dict[str, Any]
    if "validation" in suites:
        suite["validation_ref"] = "scenarios/validation.yaml"
    if "holdout" in suites:
        suite["holdout_ref"] = "scenarios/holdout.yaml"

    optimizer = {
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
    if objective is not None:
        optimizer["objective"] = dict(objective)

    metadata = {
        "name": name,
        "version": version,
        "description": description,
        "labels": {
            "aceval.generated": "true",
            "aceval.pack_type": pack_type,
            "aceval.requested_type": requested_type,
        },
        "calibration_status": CALIBRATION_DRAFT,
        "generated_by": GENERATOR_ID,
        "generation_warning": (
            "Generated evaluators are untrusted until cases and oracles are "
            "reviewed and the Pack is explicitly frozen."
        ),
        "template_fallback": (
            requested_type if requested_type != pack_type else None
        ),
        "objective_origin": objective_origin,
    }
    if test_design is not None:
        graph = test_design["capability_graph"]
        metadata["test_design"] = {
            "api_version": TEST_DESIGN_API_VERSION,
            "source_subject_hash": graph.get("subject_hash"),
            "capability_graph_ref": "design/capability-graph.json",
            "test_plan_ref": "design/test-plan.json",
            "coverage_target_ref": "design/coverage-target.json",
            "generation_provenance_ref": "design/generation-provenance.json",
        }

    return {
        "api_version": LATEST_API_VERSION,
        "kind": "EvalPack",
        "metadata": metadata,
        "subject_contract": {
            "kinds": ["skill"],
            "adapter": "skill_markdown_v1",
            "entrypoint": "SKILL.md",
        },
        "driver": _driver_for(pack_type),
        "suite": suite,
        "graders": _graders_for(pack_type, schema_path),
        "optimizer_policy": optimizer,
    }


def _driver_for(pack_type: str) -> Dict[str, Any]:
    driver = "artifact_workspace" if pack_type == "csv-summary" else "repository_workspace"
    capabilities = [
        "fresh_session",
        "workspace_fixture",
        "canonical_trace",
        "skill_activation",
    ]
    if pack_type == "csv-summary":
        capabilities.append("artifact_output")
    return {"type": driver, "required_runtime_capabilities": capabilities}


def _graders_for(pack_type: str, schema_path: str) -> List[Dict[str, Any]]:
    output_schema = {
        "id": "output-schema",
        "type": "json_schema",
        "hard": True,
        "params": {"schema_ref": schema_path},
    }
    if pack_type == "generic":
        return [
            output_schema,
            {
                "id": "expected-output",
                "type": "json_path",
                "hard": True,
                "params": {"path": "$", "equals_from_oracle": "$.expected_output"},
            },
        ]
    if pack_type == "csv-summary":
        output_schema["params"]["artifact_path"] = "summary.json"
        return [
            {
                "id": "summary-artifact",
                "type": "artifact_exists",
                "hard": True,
                "params": {
                    "path": "summary.json",
                    "min_bytes": 2,
                    "max_bytes": 1048576,
                    "mime": "application/json",
                },
            },
            output_schema,
            {
                "id": "expected-output",
                "type": "json_path",
                "hard": True,
                "params": {
                    "artifact_path": "summary.json",
                    "path": "$",
                    "equals_from_oracle": "$.expected_output",
                },
            },
            {
                "id": "workspace-policy",
                "type": "workspace_diff",
                "hard": True,
                "params": {
                    "allowed_paths": ["summary.json"],
                    "required_paths": ["summary.json"],
                },
            },
        ]
    return [
        output_schema,
        {
            "id": "expected-records",
            "type": "record_match",
            "hard": True,
            "params": {
                "collection_path": "$.findings",
                "expected_key": "expected_records",
                "forbidden_key": "forbidden_records",
                "match_fields": ["rule_id", "file", "line"],
            },
        },
        {
            "id": "source-grounding",
            "type": "source_reference",
            "hard": True,
            "params": {
                "collection_path": "$.findings",
                "file_field": "file",
                "line_field": "line",
                "allow_empty": True,
            },
        },
        {
            "id": "file-inspection-trace",
            "type": "trace_assert",
            "hard": True,
            "params": {"required_tools": ["read_file"], "min_calls": 1},
        },
    ]


def _schema_for(pack_type: str) -> Tuple[str, Dict[str, Any]]:
    if pack_type == "security-review":
        return "schemas/review-output.schema.json", {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["findings"],
            "additionalProperties": False,
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "rule_id",
                            "severity",
                            "file",
                            "line",
                            "evidence",
                            "recommendation",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "rule_id": {"type": "string", "minLength": 1},
                            "severity": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "critical"],
                            },
                            "file": {"type": "string", "minLength": 1},
                            "line": {"type": "integer", "minimum": 1},
                            "evidence": {"type": "string", "minLength": 1},
                            "recommendation": {"type": "string", "minLength": 1},
                        },
                    },
                }
            },
        }
    if pack_type == "csv-summary":
        return "schemas/summary.schema.json", {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "description": "Draft structural gate; expected_output supplies case semantics.",
            "type": "object",
        }
    return "schemas/output.schema.json", {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "description": "Draft JSON gate; expected_output supplies case semantics.",
    }


def _oracle_for(case: _Case, pack_type: str) -> Dict[str, Any]:
    if pack_type == "security-review":
        return {
            "expected_records": [dict(item) for item in case.expected_records],
            "forbidden_records": [dict(item) for item in case.forbidden_records],
        }
    if case.has_expected_output:
        return {"expected_output": case.expected_output}
    return {
        "calibration_required": True,
        "reason": "No expected_output was supplied for this generated case.",
    }


def _scenario_for(
    case: _Case,
    pack_type: str,
    fixture_refs: Sequence[str],
    workspace_paths: Sequence[str],
    oracle_ref: str,
) -> Dict[str, Any]:
    grader_ids = {
        "generic": ["output-schema", "expected-output"],
        "csv-summary": [
            "summary-artifact",
            "output-schema",
            "expected-output",
            "workspace-policy",
        ],
        "security-review": [
            "output-schema",
            "expected-records",
            "source-grounding",
            "file-inspection-trace",
        ],
    }[pack_type]
    metadata = dict(case.metadata)
    if pack_type == "csv-summary":
        configured = metadata.get("artifact_paths")
        if configured is not None and configured != ["summary.json"]:
            raise PackBuilderError(
                "%s metadata.artifact_paths conflicts with the csv-summary template"
                % case.id
            )
        metadata["artifact_paths"] = ["summary.json"]
    scenario = {
        "id": case.id,
        "split": case.split,
        "prompt": case.prompt,
        "fixtures": list(fixture_refs),
        "oracle_ref": oracle_ref,
        "grader_ids": grader_ids,
        "grader_params": {},
        "timeout_seconds": case.timeout_seconds,
        "tags": list(case.tags),
        "metadata": metadata,
    }
    if pack_type == "csv-summary":
        scenario["grader_params"] = {
            "workspace-policy": {"forbidden_paths": list(workspace_paths)}
        }
    return scenario


def _materialize_fixtures(
    root: Path, case: _Case, source_root: Path
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    refs = []
    workspace_paths = set()
    for index, fixture in enumerate(case.fixtures):
        display_path, source, content = _fixture_spec(
            fixture, source_root, "case %s fixture %d" % (case.id, index)
        )
        relative = _safe_relative(display_path, "fixture path")
        destination = root / "fixtures" / case.split / case.id / relative
        if destination.exists():
            raise PackBuilderError("duplicate fixture path in case %s: %s" % (case.id, relative))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            destination.write_bytes(content)
        elif source is not None:
            _copy_fixture_source(source, destination, source_root)
        else:  # pragma: no cover - guarded by _fixture_spec
            raise AssertionError("fixture has no source or content")
        ref = destination.relative_to(root).as_posix()
        refs.append(ref)
        for target in _fixture_workspace_paths(destination):
            if target in workspace_paths:
                raise PackBuilderError(
                    "case %s fixtures collide in the runtime workspace: %s"
                    % (case.id, target)
                )
            workspace_paths.add(target)
    return tuple(refs), tuple(sorted(workspace_paths))


def _fixture_spec(
    fixture: Any, source_root: Path, path: str
) -> Tuple[str, Optional[Path], Optional[bytes]]:
    if isinstance(fixture, str):
        relative = _safe_relative(fixture, path)
        source = source_root.joinpath(*relative.parts)
        return relative.as_posix(), source, None
    if not isinstance(fixture, Mapping):
        raise PackBuilderError("%s must be a string or object" % path)
    allowed = {"path", "source", "content", "content_base64"}
    unknown = set(fixture).difference(allowed)
    if unknown:
        raise PackBuilderError(
            "%s contains unsupported field(s): %s"
            % (path, ", ".join(sorted(str(item) for item in unknown)))
        )
    display = _safe_relative(fixture.get("path"), path + ".path")
    choices = [key for key in ("source", "content", "content_base64") if key in fixture]
    if len(choices) != 1:
        raise PackBuilderError(
            "%s requires exactly one of source, content, or content_base64" % path
        )
    if "source" in fixture:
        source_relative = _safe_relative(fixture["source"], path + ".source")
        return display.as_posix(), source_root.joinpath(*source_relative.parts), None
    if "content" in fixture:
        content = fixture["content"]
        if not isinstance(content, str):
            raise PackBuilderError("%s.content must be a string" % path)
        return display.as_posix(), None, content.encode("utf-8")
    encoded = fixture["content_base64"]
    if not isinstance(encoded, str):
        raise PackBuilderError("%s.content_base64 must be a string" % path)
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise PackBuilderError("%s.content_base64 is invalid" % path) from exc
    return display.as_posix(), None, content


def _copy_fixture_source(source: Path, destination: Path, source_root: Path) -> None:
    source_root = source_root.resolve()
    try:
        relative = source.relative_to(source_root)
    except ValueError as exc:
        raise PackBuilderError("fixture source escapes source_root: %s" % source) from exc
    current = source_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PackBuilderError("fixture sources cannot use symlinks: %s" % current)
    try:
        source = source.resolve(strict=True)
    except (OSError, FileNotFoundError) as exc:
        raise PackBuilderError("fixture source does not exist: %s" % source) from exc
    try:
        source.relative_to(source_root)
    except ValueError as exc:
        raise PackBuilderError("fixture source escapes source_root: %s" % source) from exc
    if source.is_file():
        shutil.copyfile(str(source), str(destination), follow_symlinks=False)
        return
    if not source.is_dir():
        raise PackBuilderError("fixture source must be a file or directory: %s" % source)
    destination.mkdir(parents=True, exist_ok=False)
    for item in sorted(source.rglob("*")):
        if item.is_symlink():
            raise PackBuilderError("fixture directories cannot contain symlinks: %s" % item)
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(item), str(target), follow_symlinks=False)
        else:
            raise PackBuilderError("fixture contains a non-regular file: %s" % item)


def _fixture_workspace_paths(path: Path) -> Iterable[str]:
    if path.is_file():
        return (path.name,)
    return tuple(
        item.relative_to(path).as_posix()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    )


def _validate_calibration_readiness(pack: Any, pack_type: str) -> None:
    missing = []
    invalid = []
    for scenario in pack.scenarios:
        oracle = scenario.oracle
        data = oracle.data if oracle is not None else None
        if not isinstance(data, Mapping):
            missing.append(scenario.id)
            continue
        if "expected-output" in scenario.grader_ids and "expected_output" not in data:
            missing.append(scenario.id)
        elif (
            pack_type == "csv-summary"
            and "expected-output" in scenario.grader_ids
            and not isinstance(data.get("expected_output"), Mapping)
        ):
            invalid.append("%s expected_output must be an object" % scenario.id)
        if "expected-records" in scenario.grader_ids:
            expected = data.get("expected_records", ())
            forbidden = data.get("forbidden_records", ())
            if not expected and not forbidden:
                missing.append(scenario.id)
            for record in tuple(expected or ()) + tuple(forbidden or ()):
                if not isinstance(record, Mapping) or any(
                    field not in record for field in ("rule_id", "file", "line")
                ):
                    invalid.append(
                        "%s security records require rule_id, file, and line"
                        % scenario.id
                    )
                    break
    if missing:
        raise PackCalibrationError(
            "cannot freeze: generated cases still lack semantic oracle assertions: %s"
            % ", ".join(sorted(set(missing)))
        )
    if invalid:
        raise PackCalibrationError(
            "cannot freeze: generated oracle assertions do not match the template: %s"
            % "; ".join(sorted(set(invalid)))
        )
    if pack_type not in PACK_TYPES:
        raise PackCalibrationError("unknown generated Pack type: %s" % pack_type)


def _generated_pack_type(document: Mapping[str, Any]) -> str:
    metadata = document.get("metadata", {})
    labels = metadata.get("labels", {}) if isinstance(metadata, Mapping) else {}
    pack_type = labels.get("aceval.pack_type") if isinstance(labels, Mapping) else None
    if pack_type not in PACK_TYPES:
        raise PackCalibrationError("Pack is missing a recognized generated Pack type")
    return str(pack_type)


def _generated_requested_type(
    document: Mapping[str, Any], fallback: str
) -> str:
    metadata = document.get("metadata", {})
    labels = metadata.get("labels", {}) if isinstance(metadata, Mapping) else {}
    value = labels.get("aceval.requested_type") if isinstance(labels, Mapping) else None
    return str(value) if isinstance(value, str) and value else fallback


def _evaluation_semantics(document: Mapping[str, Any]) -> Dict[str, Any]:
    value = copy.deepcopy(dict(document))
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("calibration_status", None)
    return value


def _read_generated_manifest(path: Path) -> Tuple[bytes, Dict[str, Any]]:
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackCalibrationError(
            "freeze supports deterministic JSON-form pack.yaml files generated by Pack Builder: %s"
            % exc
        ) from exc
    if not isinstance(document, dict):
        raise PackCalibrationError("pack.yaml must contain an object")
    if document.get("metadata", {}).get("generated_by") != GENERATOR_ID:
        raise PackCalibrationError("Pack was not generated by %s" % GENERATOR_ID)
    return raw, document


def _safe_relative(value: Any, path: str) -> PurePath:
    if not isinstance(value, str) or not value.strip():
        raise PackBuilderError("%s must be a non-empty relative path" % path)
    text = value.strip()
    pure = PurePath(text)
    windows = PureWindowsPath(text)
    if (
        "\x00" in text
        or pure.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in pure.parts
        or ".." in windows.parts
        or text == "."
    ):
        raise PackBuilderError("%s must stay inside its declared root" % path)
    return pure


def _component_id(value: Any, path: str) -> str:
    text = _non_empty_string(value, path)
    if not _COMPONENT_ID.match(text):
        raise PackBuilderError(
            "%s must match %s; got %r" % (path, _COMPONENT_ID.pattern, text)
        )
    return text


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PackBuilderError("%s must be a non-empty string" % path)
    return value.strip()


def _json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PackBuilderError("generated Pack contains a non-JSON value: %s" % exc) from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s-" % path.name, dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


# Short aliases are convenient integration hooks for a future CLI.
generate_pack = generate_evalpack
freeze_pack = freeze_evalpack


__all__ = [
    "PACK_TYPES",
    "CALIBRATION_CALIBRATING",
    "CALIBRATION_DRAFT",
    "CALIBRATION_FROZEN",
    "GENERATOR_ID",
    "PackBuilderError",
    "PackCalibrationError",
    "PackBuildResult",
    "generate_evalpack",
    "infer_objective_from_goal",
    "begin_calibration",
    "freeze_evalpack",
    "calibration_status",
    "generate_pack",
    "freeze_pack",
]
