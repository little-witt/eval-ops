"""Experiment control plane kept separate from frozen evaluation suites.

EvalPacks define what is evaluated.  ExperimentPlans define how a Skill may be
optimized against that frozen suite.  The separation prevents candidate
generation policy, goals, and patch budgets from changing evaluator hashes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PureWindowsPath
import re
import tempfile
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from .contracts import (
    FrozenEvalPack,
    ImprovementMode,
    MetricDirection,
    MetricSourceSpec,
    ObjectiveSpec,
    OptimizerPolicySpec,
)


EXPERIMENT_PLAN_API_VERSION = "aceval.experiment-plan/v1"
EXPERIMENT_PLAN_KIND = "ExperimentPlan"
MAX_EXPERIMENT_PLAN_BYTES = 1024 * 1024
_COMPONENT_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ENV_SPLITS = frozenset(("dev", "validation", "holdout"))


class ExperimentPlanError(ValueError):
    """Raised when an ExperimentPlan is unsafe, malformed, or mismatched."""


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: %s" % key)
        result[key] = value
    return result


def _strict_document(path: Path) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExperimentPlanError("cannot read ExperimentPlan") from exc
    if len(raw) > MAX_EXPERIMENT_PLAN_BYTES:
        raise ExperimentPlanError("ExperimentPlan exceeds byte limit")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError("non-finite JSON number: %s" % item)
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExperimentPlanError("ExperimentPlan must be strict UTF-8 JSON") from exc
    return _mapping(value, "ExperimentPlan")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ExperimentPlanError("%s must be an object with string keys" % label)
    return value


def _check_keys(value: Mapping[str, Any], allowed: Sequence[str], label: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ExperimentPlanError(
            "%s contains unsupported fields: %s" % (label, ", ".join(unknown))
        )


def _string(value: Any, label: str, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if (
        not isinstance(value, str)
        or (required and not value)
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise ExperimentPlanError("%s must be a trimmed safe string" % label)
    return value


def _component(value: Any, label: str) -> str:
    text = _string(value, label)
    if not _COMPONENT_ID.fullmatch(text):
        raise ExperimentPlanError("%s must be a component id" % label)
    return text


def _strings(value: Any, label: str) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExperimentPlanError("%s must be an array" % label)
    return tuple(_string(item, "%s[%d]" % (label, index)) for index, item in enumerate(value))


def _positive_integer(value: Any, label: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExperimentPlanError("%s must be a positive integer" % label)
    return value


def _non_negative_integer(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExperimentPlanError("%s must be a non-negative integer" % label)
    return value


def _number(value: Any, label: str, default: float = 0.0) -> float:
    if value is None:
        value = default
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ExperimentPlanError("%s must be a finite number" % label)
    return float(value)


def _subject_path(value: str, label: str) -> str:
    pure = PurePath(value)
    windows = PureWindowsPath(value)
    if (
        "\x00" in value
        or not value
        or pure.is_absolute()
        or os.path.isabs(value)
        or windows.is_absolute()
        or windows.drive
        or ".." in pure.parts
        or ".." in windows.parts
    ):
        raise ExperimentPlanError("%s must be a safe Subject-relative path" % label)
    return value


def objective_from_mapping(value: Any) -> Optional[ObjectiveSpec]:
    if value is None:
        return None
    item = _mapping(value, "optimization.objective")
    _check_keys(
        item,
        ("id", "source", "direction", "aggregation", "min_delta", "target", "max_case_regression"),
        "optimization.objective",
    )
    source_value = _mapping(item.get("source"), "optimization.objective.source")
    _check_keys(source_value, ("type", "grader_id", "key"), "optimization.objective.source")
    try:
        direction = MetricDirection(item.get("direction", MetricDirection.MAXIMIZE.value))
    except ValueError as exc:
        raise ExperimentPlanError("objective direction must be maximize or minimize") from exc
    source = MetricSourceSpec(
        type=_component(source_value.get("type"), "optimization.objective.source.type"),
        grader_id=(
            _component(source_value.get("grader_id"), "optimization.objective.source.grader_id")
            if source_value.get("grader_id") is not None
            else None
        ),
        key=(
            _component(source_value.get("key"), "optimization.objective.source.key")
            if source_value.get("key") is not None
            else None
        ),
    )
    try:
        return ObjectiveSpec(
            id=_component(item.get("id"), "optimization.objective.id"),
            source=source,
            direction=direction,
            aggregation=_string(item.get("aggregation", "mean"), "optimization.objective.aggregation"),
            min_delta=_number(item.get("min_delta"), "optimization.objective.min_delta"),
            target=(
                _number(item.get("target"), "optimization.objective.target")
                if item.get("target") is not None
                else None
            ),
            max_case_regression=_number(
                item.get("max_case_regression"),
                "optimization.objective.max_case_regression",
            ),
        )
    except ValueError as exc:
        raise ExperimentPlanError(str(exc)) from exc


def policy_from_mapping(value: Any) -> OptimizerPolicySpec:
    item = _mapping(value, "optimization")
    _check_keys(
        item,
        (
            "adapter", "patchable_components", "allowed_paths", "visible_splits",
            "beam_width", "max_rounds", "max_candidate_snapshots", "max_added_lines",
            "forbid_case_literals", "mode", "goal", "objective", "params",
        ),
        "optimization",
    )
    allowed_paths = _strings(item.get("allowed_paths", ()), "optimization.allowed_paths")
    for index, path in enumerate(allowed_paths):
        _subject_path(path, "optimization.allowed_paths[%d]" % index)
    visible_splits = _strings(item.get("visible_splits", ("dev",)), "optimization.visible_splits")
    invalid_splits = sorted(set(visible_splits) - _ENV_SPLITS)
    if invalid_splits:
        raise ExperimentPlanError("optimization.visible_splits contains unknown splits")
    forbid = item.get("forbid_case_literals", True)
    if not isinstance(forbid, bool):
        raise ExperimentPlanError("optimization.forbid_case_literals must be boolean")
    params = _mapping(item.get("params", {}), "optimization.params")
    try:
        mode = ImprovementMode(item.get("mode", ImprovementMode.AUTO.value))
    except ValueError as exc:
        raise ExperimentPlanError("optimization.mode must be auto, repair, or tune") from exc
    try:
        return OptimizerPolicySpec(
            adapter=_component(item.get("adapter"), "optimization.adapter"),
            patchable_components=_strings(
                item.get("patchable_components", ()),
                "optimization.patchable_components",
            ),
            allowed_paths=allowed_paths,
            visible_splits=visible_splits,
            beam_width=_positive_integer(item.get("beam_width"), "optimization.beam_width", 1),
            max_rounds=_positive_integer(item.get("max_rounds"), "optimization.max_rounds", 1),
            max_candidate_snapshots=_positive_integer(
                item.get("max_candidate_snapshots"),
                "optimization.max_candidate_snapshots",
                1,
            ),
            max_added_lines=_non_negative_integer(
                item.get("max_added_lines"), "optimization.max_added_lines"
            ),
            forbid_case_literals=forbid,
            mode=mode,
            goal=_string(item.get("goal", ""), "optimization.goal", required=False),
            objective=objective_from_mapping(item.get("objective")),
            params=dict(params),
        )
    except ValueError as exc:
        raise ExperimentPlanError(str(exc)) from exc


def objective_to_dict(value: Optional[ObjectiveSpec]) -> Optional[Mapping[str, Any]]:
    if value is None:
        return None
    return {
        "id": value.id,
        "source": {
            "type": value.source.type,
            "grader_id": value.source.grader_id,
            "key": value.source.key,
        },
        "direction": value.direction.value,
        "aggregation": value.aggregation,
        "min_delta": value.min_delta,
        "target": value.target,
        "max_case_regression": value.max_case_regression,
    }


def policy_to_dict(value: OptimizerPolicySpec) -> Mapping[str, Any]:
    return {
        "adapter": value.adapter,
        "patchable_components": list(value.patchable_components),
        "allowed_paths": list(value.allowed_paths),
        "visible_splits": list(value.visible_splits),
        "beam_width": value.beam_width,
        "max_rounds": value.max_rounds,
        "max_candidate_snapshots": value.max_candidate_snapshots,
        "max_added_lines": value.max_added_lines,
        "forbid_case_literals": value.forbid_case_literals,
        "mode": value.mode.value,
        "goal": value.goal,
        "objective": objective_to_dict(value.objective),
        "params": dict(value.params),
    }


@dataclass(frozen=True)
class ExperimentPlan:
    name: str
    suite_hash: str
    optimization: OptimizerPolicySpec
    source: str = "external"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    api_version: str = EXPERIMENT_PLAN_API_VERSION
    kind: str = EXPERIMENT_PLAN_KIND

    def __post_init__(self) -> None:
        if self.api_version != EXPERIMENT_PLAN_API_VERSION:
            raise ExperimentPlanError("unsupported ExperimentPlan api_version")
        if self.kind != EXPERIMENT_PLAN_KIND:
            raise ExperimentPlanError("ExperimentPlan kind is invalid")
        object.__setattr__(self, "name", _component(self.name, "metadata.name"))
        suite_hash = str(self.suite_hash)
        if suite_hash.startswith("sha256:"):
            suite_hash = suite_hash[7:]
        if not _HASH.fullmatch(suite_hash):
            raise ExperimentPlanError("eval_suite.suite_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "suite_hash", suite_hash)
        if not isinstance(self.optimization, OptimizerPolicySpec):
            raise ExperimentPlanError("optimization must be an OptimizerPolicySpec")
        object.__setattr__(self, "source", _string(self.source, "metadata.source"))
        metadata = _mapping(self.metadata, "metadata")
        if "name" in metadata or "source" in metadata:
            raise ExperimentPlanError(
                "metadata extras cannot override name or source"
            )
        normalized_metadata = dict(metadata)
        try:
            json.dumps(normalized_metadata, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ExperimentPlanError(
                "metadata must contain finite JSON values"
            ) from exc
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(normalized_metadata),
        )

    @property
    def content_hash(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> Mapping[str, Any]:
        metadata = {"name": self.name, "source": self.source}
        metadata.update(dict(self.metadata))
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "metadata": metadata,
            "eval_suite": {"suite_hash": self.suite_hash},
            "optimization": policy_to_dict(self.optimization),
        }

    def validate_suite(self, pack: FrozenEvalPack) -> None:
        if not isinstance(pack, FrozenEvalPack):
            raise TypeError("pack must be a FrozenEvalPack")
        if self.suite_hash != pack.suite_hash:
            raise ExperimentPlanError(
                "ExperimentPlan suite_hash does not match the loaded EvalSuite"
            )

    @classmethod
    def from_mapping(cls, value: Any) -> "ExperimentPlan":
        root = _mapping(value, "ExperimentPlan")
        _check_keys(root, ("api_version", "kind", "metadata", "eval_suite", "optimization"), "ExperimentPlan")
        metadata = _mapping(root.get("metadata"), "metadata")
        if "name" not in metadata:
            raise ExperimentPlanError("metadata.name is required")
        suite = _mapping(root.get("eval_suite"), "eval_suite")
        _check_keys(suite, ("suite_hash",), "eval_suite")
        return cls(
            api_version=root.get("api_version"),
            kind=root.get("kind"),
            name=metadata.get("name"),
            source=metadata.get("source", "external"),
            metadata={key: item for key, item in metadata.items() if key not in ("name", "source")},
            suite_hash=suite.get("suite_hash"),
            optimization=policy_from_mapping(root.get("optimization")),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "ExperimentPlan":
        return cls.from_mapping(_strict_document(Path(path)))


def legacy_experiment_plan(pack: FrozenEvalPack) -> ExperimentPlan:
    policy = pack.manifest.optimizer_policy
    if policy is None:
        raise ExperimentPlanError(
            "EvalSuite has no ExperimentPlan and no legacy optimizer_policy"
        )
    return ExperimentPlan(
        name="%s-legacy" % pack.manifest.metadata.name,
        suite_hash=pack.suite_hash,
        optimization=policy,
        source="legacy_pack",
        metadata={"legacy_pack_hash": pack.pack_hash},
    )


def adjacent_experiment_path(pack_root: Union[str, Path]) -> Path:
    root = Path(pack_root).expanduser().resolve()
    return root.parent / (root.name + ".experiment.json")


def resolve_experiment_plan(
    pack: FrozenEvalPack,
    path: Optional[Union[str, Path]] = None,
) -> ExperimentPlan:
    if path is not None:
        plan = ExperimentPlan.load(path)
    else:
        adjacent = adjacent_experiment_path(pack.root)
        plan = ExperimentPlan.load(adjacent) if adjacent.is_file() else legacy_experiment_plan(pack)
    plan.validate_suite(pack)
    return plan


def write_experiment_plan(
    path: Union[str, Path],
    plan: ExperimentPlan,
    *,
    overwrite: bool = False,
) -> Path:
    if not isinstance(plan, ExperimentPlan):
        raise TypeError("plan must be an ExperimentPlan")
    target = Path(path).expanduser().resolve(strict=False)
    if target.is_symlink():
        raise ExperimentPlanError("ExperimentPlan output cannot be a symlink")
    if target.exists() and not overwrite:
        raise ExperimentPlanError("ExperimentPlan output already exists: %s" % target)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s-" % target.name,
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return target


__all__ = [
    "EXPERIMENT_PLAN_API_VERSION",
    "EXPERIMENT_PLAN_KIND",
    "ExperimentPlanError",
    "ExperimentPlan",
    "objective_from_mapping",
    "policy_from_mapping",
    "objective_to_dict",
    "policy_to_dict",
    "legacy_experiment_plan",
    "adjacent_experiment_path",
    "resolve_experiment_plan",
    "write_experiment_plan",
]
