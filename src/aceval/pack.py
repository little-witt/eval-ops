from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .contracts import (
    ComponentRegistryProtocol,
    DriverSpec,
    EvalPackManifest,
    FrozenEvalPack,
    FrozenOracle,
    FrozenScenario,
    GraderSpec,
    OptimizerPolicySpec,
    PackIssue,
    PackMetadata,
    PackRef,
    PackReport,
    Scenario,
    ScenarioSplit,
    SubjectContract,
    SuiteSpec,
    as_primitive,
)


SUPPORTED_API_VERSION = "aceval.dev/v1alpha1"
PACK_KIND = "EvalPack"
_COMPONENT_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SPLITS = frozenset(split.value for split in ScenarioSplit)


class PackError(ValueError):
    """Base class for errors that make a Pack unusable."""


class PackFormatError(PackError):
    pass


class PackPathError(PackError):
    pass


class MissingYamlDependencyError(PackError):
    pass


class UnknownComponentError(PackError, KeyError):
    def __init__(self, category: str, component_id: str):
        self.category = category
        self.component_id = component_id
        super().__init__("unknown {0} component: {1}".format(category, component_id))


class DuplicateComponentError(PackError):
    pass


class UnsupportedApiVersionError(PackFormatError):
    pass


class PackValidationError(PackError):
    def __init__(self, report: PackReport):
        self.report = report
        details = "; ".join(
            "{0}: {1}".format(issue.code, issue.message) for issue in report.errors
        )
        super().__init__(details or "EvalPack validation failed")


def _component_id(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PackFormatError("{0} must be a non-empty string".format(path))
    value = value.strip()
    if not _COMPONENT_ID.match(value):
        raise PackFormatError(
            "{0} has invalid component id {1!r}".format(path, value)
        )
    return value


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PackFormatError("{0} must be a mapping".format(path))
    return value


def _string(value: Any, path: str, required: bool = True) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise PackFormatError("{0} must be a non-empty string".format(path))
    return value.strip()


def _strings(value: Any, path: str, allow_none: bool = False) -> Tuple[str, ...]:
    if value is None and allow_none:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise PackFormatError("{0} must be a list of strings".format(path))
    result = []
    for index, item in enumerate(value):
        result.append(_string(item, "{0}[{1}]".format(path, index)) or "")
    return tuple(result)


def _integer(value: Any, path: str, default: Optional[int] = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise PackFormatError("{0} must be an integer".format(path))
    return value


def _boolean(value: Any, path: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PackFormatError("{0} must be a boolean".format(path))
    return value


def _check_keys(value: Mapping[str, Any], allowed: Iterable[str], path: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise PackFormatError(
            "{0} contains unsupported field(s): {1}".format(path, ", ".join(unknown))
        )


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_json(value: Any) -> str:
    payload = json.dumps(
        as_primitive(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return _hash_bytes(payload)


def _read_document(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PackFormatError("cannot read {0}: {1}".format(path, exc)) from exc
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except _NonStandardJsonConstant as exc:
        raise PackFormatError(
            "non-standard JSON number in {0}: {1}".format(path, exc)
        ) from exc
    except json.JSONDecodeError as json_error:
        try:
            yaml = importlib.import_module("yaml")
        except ModuleNotFoundError as exc:
            raise MissingYamlDependencyError(
                "{0} is not JSON. Install PyYAML (`pip install PyYAML` or "
                "`pip install aceval[yaml]`) to load YAML Pack files.".format(path)
            ) from exc
        try:
            value = yaml.safe_load(text)
        except Exception as exc:
            raise PackFormatError(
                "cannot parse YAML {0}: {1}".format(path, exc)
            ) from exc
    _reject_non_finite(value, str(path))
    return value


class _NonStandardJsonConstant(ValueError):
    pass


def _reject_json_constant(value: str) -> None:
    raise _NonStandardJsonConstant("non-standard JSON constant: %s" % value)


def _reject_non_finite(value: Any, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise PackFormatError("non-finite number in %s" % path)
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_non_finite(key, path)
            _reject_non_finite(item, path)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_finite(item, path)


def _safe_relative_reference(value: Any, path: str) -> str:
    reference = _string(value, path) or ""
    candidate = PurePath(reference)
    windows = PureWindowsPath(reference)
    if "\x00" in reference:
        raise PackPathError("{0} cannot contain a NUL byte".format(path))
    if candidate.is_absolute() or os.path.isabs(reference) or windows.is_absolute() or windows.drive:
        raise PackPathError("{0} must be relative to the Pack root".format(path))
    if ".." in candidate.parts or ".." in windows.parts:
        raise PackPathError("{0} cannot contain '..': {1}".format(path, reference))
    if not reference or reference == ".":
        raise PackPathError("{0} cannot be empty or the Pack root".format(path))
    return reference


def _safe_pack_path(
    root: Path, value: Any, path: str, expect: Optional[str] = None
) -> Path:
    reference = _safe_relative_reference(value, path)
    candidate = root.joinpath(*PurePath(reference).parts)
    if candidate.is_symlink():
        raise PackPathError(
            "{0} points through a symlink; Pack references must not use symlinks: {1}".format(
                path, reference
            )
        )
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PackPathError(
            "{0} does not exist inside Pack root: {1}".format(path, reference)
        ) from exc
    except OSError as exc:
        raise PackPathError("cannot resolve {0}: {1}".format(path, exc)) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PackPathError(
            "{0} escapes Pack root: {1}".format(path, reference)
        ) from exc
    if expect == "file" and not resolved.is_file():
        raise PackPathError("{0} must reference a file: {1}".format(path, reference))
    if expect == "directory" and not resolved.is_dir():
        raise PackPathError(
            "{0} must reference a directory: {1}".format(path, reference)
        )
    return resolved


def _assert_safe_tree(root: Path) -> None:
    if root.is_symlink():
        raise PackPathError("Pack root itself cannot be a symlink: {0}".format(root))
    try:
        entries = root.rglob("*")
        for entry in entries:
            if entry.is_symlink():
                raise PackPathError(
                    "Pack contains a symlink, which is not allowed: {0}".format(
                        entry.relative_to(root)
                    )
                )
    except OSError as exc:
        raise PackPathError("cannot inspect Pack tree {0}: {1}".format(root, exc)) from exc


def _hash_reference(path: Path) -> str:
    if path.is_file():
        return _hash_bytes(path.read_bytes())
    records = []
    for entry in sorted(path.rglob("*")):
        if entry.is_symlink():
            raise PackPathError("symlink encountered while hashing: {0}".format(entry))
        if entry.is_file():
            records.append((entry.relative_to(path).as_posix(), _hash_bytes(entry.read_bytes())))
    return _hash_json(records)


def _hash_tree(root: Path) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            raise PackPathError("symlink encountered while hashing: {0}".format(entry))
        if entry.is_file():
            hashes[entry.relative_to(root).as_posix()] = _hash_bytes(entry.read_bytes())
    return hashes


def _validate_manifest_local_refs(root: Path, manifest: EvalPackManifest) -> None:
    for index, grader in enumerate(manifest.graders):
        for key in ("schema_ref", "rubric_ref"):
            reference = grader.params.get(key)
            if reference is not None:
                _safe_pack_path(
                    root,
                    reference,
                    "manifest.graders[{0}].params.{1}".format(index, key),
                    "file",
                )


def _load_manifest_resources(
    root: Path, manifest: EvalPackManifest
) -> Dict[str, Any]:
    resources: Dict[str, Any] = {}
    for index, grader in enumerate(manifest.graders):
        for key in ("schema_ref", "rubric_ref"):
            value = grader.params.get(key)
            if value is None:
                continue
            reference = _safe_relative_reference(
                value, "manifest.graders[{0}].params.{1}".format(index, key)
            )
            if reference in resources:
                continue
            path = _safe_pack_path(
                root,
                reference,
                "manifest.graders[{0}].params.{1}".format(index, key),
                "file",
            )
            if key == "schema_ref":
                resources[reference] = _read_document(path)
            else:
                try:
                    resources[reference] = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise PackFormatError(
                        "cannot read manifest Grader resource {0}: {1}".format(
                            reference, exc
                        )
                    ) from exc
    return resources


def _parse_manifest(document: Any) -> EvalPackManifest:
    value = _mapping(document, "manifest")
    _check_keys(
        value,
        {
            "api_version",
            "kind",
            "metadata",
            "subject_contract",
            "driver",
            "suite",
            "graders",
            "optimizer_policy",
        },
        "manifest",
    )
    api_version = _string(value.get("api_version"), "manifest.api_version") or ""
    if api_version != SUPPORTED_API_VERSION:
        raise UnsupportedApiVersionError(
            "unsupported api_version {0!r}; expected {1}".format(
                api_version, SUPPORTED_API_VERSION
            )
        )
    kind = _string(value.get("kind"), "manifest.kind") or ""
    if kind != PACK_KIND:
        raise PackFormatError(
            "manifest.kind must be {0!r}, got {1!r}".format(PACK_KIND, kind)
        )

    metadata_value = _mapping(value.get("metadata"), "manifest.metadata")
    if "name" not in metadata_value or "version" not in metadata_value:
        raise PackFormatError("manifest.metadata requires name and version")
    metadata_labels = metadata_value.get("labels", {})
    metadata_labels = _mapping(metadata_labels, "manifest.metadata.labels")
    labels: Dict[str, str] = {}
    for key, label in metadata_labels.items():
        labels[str(key)] = _string(label, "manifest.metadata.labels.{0}".format(key)) or ""
    metadata_extra = {
        str(key): item
        for key, item in metadata_value.items()
        if key not in {"name", "version", "description", "labels"}
    }
    metadata = PackMetadata(
        name=_string(metadata_value.get("name"), "manifest.metadata.name") or "",
        version=_string(metadata_value.get("version"), "manifest.metadata.version")
        or "",
        description=_string(
            metadata_value.get("description", ""), "manifest.metadata.description", False
        )
        or "",
        labels=labels,
        extra=metadata_extra,
    )

    subject_value = _mapping(value.get("subject_contract"), "manifest.subject_contract")
    _check_keys(
        subject_value,
        {"kinds", "adapter", "entrypoint", "params"},
        "manifest.subject_contract",
    )
    subject_params = _mapping(
        subject_value.get("params", {}), "manifest.subject_contract.params"
    )
    subject_contract = SubjectContract(
        kinds=_strings(subject_value.get("kinds"), "manifest.subject_contract.kinds"),
        adapter=_component_id(
            subject_value.get("adapter"), "manifest.subject_contract.adapter"
        ),
        entrypoint=_string(
            subject_value.get("entrypoint"), "manifest.subject_contract.entrypoint"
        )
        or "",
        params=subject_params,
    )
    for index, kind_value in enumerate(subject_contract.kinds):
        _component_id(kind_value, "manifest.subject_contract.kinds[{0}]".format(index))
    _validate_subject_relative(subject_contract.entrypoint, "manifest.subject_contract.entrypoint")

    driver_value = _mapping(value.get("driver"), "manifest.driver")
    _check_keys(
        driver_value,
        {"type", "required_runtime_capabilities", "params"},
        "manifest.driver",
    )
    driver = DriverSpec(
        type=_component_id(driver_value.get("type"), "manifest.driver.type"),
        required_runtime_capabilities=_strings(
            driver_value.get("required_runtime_capabilities", []),
            "manifest.driver.required_runtime_capabilities",
        ),
        params=_mapping(driver_value.get("params", {}), "manifest.driver.params"),
    )

    suite_value = _mapping(value.get("suite"), "manifest.suite")
    _check_keys(
        suite_value,
        {"dev", "validation", "holdout", "validation_ref", "holdout_ref"},
        "manifest.suite",
    )
    suite = SuiteSpec(
        dev=_string(suite_value.get("dev"), "manifest.suite.dev", False),
        validation=_string(
            suite_value.get("validation"), "manifest.suite.validation", False
        ),
        holdout=_string(suite_value.get("holdout"), "manifest.suite.holdout", False),
        validation_ref=_string(
            suite_value.get("validation_ref"), "manifest.suite.validation_ref", False
        ),
        holdout_ref=_string(
            suite_value.get("holdout_ref"), "manifest.suite.holdout_ref", False
        ),
    )
    if suite.validation is not None and suite.validation_ref is not None:
        raise PackFormatError(
            "manifest.suite cannot declare both validation and validation_ref"
        )
    if suite.holdout is not None and suite.holdout_ref is not None:
        raise PackFormatError(
            "manifest.suite cannot declare both holdout and holdout_ref"
        )
    for key, ref in (("dev", suite.dev), ("validation", suite.validation), ("holdout", suite.holdout)):
        if ref is not None:
            _safe_relative_reference(ref, "manifest.suite.{0}".format(key))
    for key, ref in (
        ("validation_ref", suite.validation_ref),
        ("holdout_ref", suite.holdout_ref),
    ):
        if ref is not None:
            pure = PurePath(ref)
            windows = PureWindowsPath(ref)
            if (
                "\x00" in ref
                or pure.is_absolute()
                or os.path.isabs(ref)
                or windows.is_absolute()
                or windows.drive
                or ".." in pure.parts
                or ".." in windows.parts
            ):
                raise PackPathError(
                    "manifest.suite.{0} cannot be absolute or contain '..'".format(key)
                )

    graders_value = value.get("graders")
    if not isinstance(graders_value, (list, tuple)):
        raise PackFormatError("manifest.graders must be a list")
    graders: List[GraderSpec] = []
    grader_ids: Set[str] = set()
    for index, item in enumerate(graders_value):
        path = "manifest.graders[{0}]".format(index)
        grader_value = _mapping(item, path)
        _check_keys(grader_value, {"id", "type", "hard", "params"}, path)
        grader_id = _component_id(grader_value.get("id"), path + ".id")
        if grader_id in grader_ids:
            raise PackFormatError("duplicate grader id: {0}".format(grader_id))
        grader_ids.add(grader_id)
        graders.append(
            GraderSpec(
                id=grader_id,
                type=_component_id(grader_value.get("type"), path + ".type"),
                hard=_boolean(grader_value.get("hard"), path + ".hard", True),
                params=_mapping(grader_value.get("params", {}), path + ".params"),
            )
        )

    optimizer = None
    if value.get("optimizer_policy") is not None:
        optimizer_value = _mapping(value.get("optimizer_policy"), "manifest.optimizer_policy")
        _check_keys(
            optimizer_value,
            {
                "adapter",
                "patchable_components",
                "allowed_paths",
                "visible_splits",
                "beam_width",
                "max_rounds",
                "max_candidate_snapshots",
                "max_added_lines",
                "forbid_case_literals",
                "params",
            },
            "manifest.optimizer_policy",
        )
        beam_width = _integer(
            optimizer_value.get("beam_width"), "manifest.optimizer_policy.beam_width", 1
        )
        max_rounds = _integer(
            optimizer_value.get("max_rounds"), "manifest.optimizer_policy.max_rounds", 1
        )
        max_snapshots = _integer(
            optimizer_value.get("max_candidate_snapshots"),
            "manifest.optimizer_policy.max_candidate_snapshots",
            1,
        )
        max_added = optimizer_value.get("max_added_lines")
        if max_added is not None:
            max_added = _integer(max_added, "manifest.optimizer_policy.max_added_lines")
        if beam_width < 1 or max_rounds < 1 or max_snapshots < 1:
            raise PackFormatError("optimizer budgets must be positive")
        if max_added is not None and max_added < 0:
            raise PackFormatError("manifest.optimizer_policy.max_added_lines cannot be negative")
        optimizer = OptimizerPolicySpec(
            adapter=_component_id(
                optimizer_value.get("adapter"), "manifest.optimizer_policy.adapter"
            ),
            patchable_components=_strings(
                optimizer_value.get("patchable_components", []),
                "manifest.optimizer_policy.patchable_components",
            ),
            allowed_paths=_strings(
                optimizer_value.get("allowed_paths", []),
                "manifest.optimizer_policy.allowed_paths",
            ),
            visible_splits=_strings(
                optimizer_value.get("visible_splits", [ScenarioSplit.DEV.value]),
                "manifest.optimizer_policy.visible_splits",
            ),
            beam_width=beam_width,
            max_rounds=max_rounds,
            max_candidate_snapshots=max_snapshots,
            max_added_lines=max_added,
            forbid_case_literals=_boolean(
                optimizer_value.get("forbid_case_literals"),
                "manifest.optimizer_policy.forbid_case_literals",
                True,
            ),
            params=_mapping(
                optimizer_value.get("params", {}), "manifest.optimizer_policy.params"
            ),
        )
        for path_value in optimizer.allowed_paths:
            _validate_subject_relative(path_value, "manifest.optimizer_policy.allowed_paths")
        for split in optimizer.visible_splits:
            if split not in _SPLITS:
                raise PackFormatError(
                    "manifest.optimizer_policy.visible_splits contains unknown split: {0}".format(
                        split
                    )
                )

    return EvalPackManifest(
        api_version=api_version,
        kind=kind,
        metadata=metadata,
        subject_contract=subject_contract,
        driver=driver,
        suite=suite,
        graders=tuple(graders),
        optimizer_policy=optimizer,
    )


def _validate_subject_relative(value: str, path: str) -> None:
    reference = _string(value, path) or ""
    pure = PurePath(reference)
    windows = PureWindowsPath(reference)
    if (
        "\x00" in reference
        or pure.is_absolute()
        or os.path.isabs(reference)
        or windows.is_absolute()
        or windows.drive
        or ".." in pure.parts
        or ".." in windows.parts
    ):
        raise PackPathError("{0} must be a safe relative subject path".format(path))


def _scenario_entries(document: Any, path: Path) -> Sequence[Mapping[str, Any]]:
    if isinstance(document, list):
        entries = document
    elif isinstance(document, Mapping) and "scenarios" in document:
        _check_keys(document, {"scenarios"}, str(path))
        entries = document["scenarios"]
    elif isinstance(document, Mapping):
        entries = [document]
    else:
        raise PackFormatError("{0} must contain a scenario mapping or scenarios list".format(path))
    if not isinstance(entries, (list, tuple)):
        raise PackFormatError("{0}.scenarios must be a list".format(path))
    result = []
    for index, entry in enumerate(entries):
        result.append(_mapping(entry, "{0}.scenarios[{1}]".format(path, index)))
    return result


def _parse_scenario(
    value: Mapping[str, Any], default_split: str, root: Path, source_path: Path
) -> Tuple[Scenario, Optional[FrozenOracle], Dict[str, str], FrozenScenario]:
    path = "{0}".format(source_path)
    _check_keys(
        value,
        {
            "id",
            "split",
            "prompt",
            "fixtures",
            "oracle_ref",
            "grader_ids",
            "grader_params",
            "timeout_seconds",
            "tags",
            "metadata",
        },
        path,
    )
    scenario_id = _component_id(value.get("id"), path + ".id")
    split = _string(value.get("split", default_split), path + ".split") or default_split
    if split not in _SPLITS:
        raise PackFormatError("{0}.split has unknown value: {1}".format(path, split))
    if split != default_split:
        raise PackFormatError(
            "scenario {0} declares split {1!r} in {2} suite file".format(
                scenario_id, split, default_split
            )
        )
    prompt = _string(value.get("prompt", ""), path + ".prompt", False) or ""
    fixtures_value = value.get("fixtures", [])
    fixtures = _strings(fixtures_value, path + ".fixtures", allow_none=True)
    fixture_hashes: Dict[str, str] = {}
    for index, fixture in enumerate(fixtures):
        fixture_path = _safe_pack_path(
            root, fixture, "{0}.fixtures[{1}]".format(path, index)
        )
        fixture_hashes[fixture] = _hash_reference(fixture_path)

    oracle = None
    oracle_ref = value.get("oracle_ref")
    if oracle_ref is not None:
        oracle_ref = _safe_relative_reference(oracle_ref, path + ".oracle_ref")
        oracle_path = _safe_pack_path(root, oracle_ref, path + ".oracle_ref", "file")
        oracle_data = _read_document(oracle_path)
        oracle = FrozenOracle(
            data=oracle_data,
            ref=oracle_ref,
            content_hash=_hash_json(oracle_data),
        )

    grader_params_value = _mapping(value.get("grader_params", {}), path + ".grader_params")
    grader_params: Dict[str, Mapping[str, Any]] = {}
    for grader_id, params in grader_params_value.items():
        if not isinstance(grader_id, str):
            raise PackFormatError("{0}.grader_params keys must be strings".format(path))
        grader_params[grader_id] = _mapping(
            params, "{0}.grader_params.{1}".format(path, grader_id)
        )
    timeout = _integer(value.get("timeout_seconds"), path + ".timeout_seconds", 120)
    if timeout <= 0:
        raise PackFormatError("{0}.timeout_seconds must be positive".format(path))
    metadata = _mapping(value.get("metadata", {}), path + ".metadata")
    scenario = Scenario(
        id=scenario_id,
        split=split,
        prompt=prompt,
        fixtures=fixtures,
        oracle_ref=oracle_ref,
        grader_ids=_strings(value.get("grader_ids", []), path + ".grader_ids", True),
        grader_params=grader_params,
        timeout_seconds=timeout,
        tags=_strings(value.get("tags", []), path + ".tags", True),
        metadata=metadata,
    )
    content_hash = _hash_json(
        {"scenario": scenario, "oracle": oracle, "fixtures": fixture_hashes}
    )
    frozen = FrozenScenario(
        scenario=scenario,
        oracle=oracle,
        fixture_hashes=fixture_hashes,
        content_hash=content_hash,
    )
    return scenario, oracle, fixture_hashes, frozen


def _lookup(registry: Any, category: str, component_id: str) -> Any:
    method = getattr(registry, category)
    try:
        return method(component_id)
    except (KeyError, UnknownComponentError):
        raise UnknownComponentError(category, component_id)


class ComponentRegistry:
    """Explicit in-process registry; it never imports components by name."""

    def __init__(self, subject_kinds: Iterable[str] = ("skill",)) -> None:
        self._subjects: Dict[str, Any] = {}
        self._drivers: Dict[str, Any] = {}
        self._graders: Dict[str, Any] = {}
        self._optimizers: Dict[str, Any] = {}
        self._subject_kinds: Set[str] = set(subject_kinds)

    @property
    def subject_kinds(self) -> Tuple[str, ...]:
        return tuple(sorted(self._subject_kinds))

    def _register(self, store: Dict[str, Any], category: str, component_id: str, component: Any) -> Any:
        component_id = _component_id(component_id, category)
        if component_id in store:
            raise DuplicateComponentError(
                "duplicate {0} component: {1}".format(category, component_id)
            )
        if component is None:
            raise PackFormatError("{0} component {1} cannot be None".format(category, component_id))
        store[component_id] = component
        return component

    def register_subject_adapter(
        self, component_id: str, component: Any, kinds: Optional[Iterable[str]] = None
    ) -> Any:
        registered = self._register(self._subjects, "subject_adapter", component_id, component)
        declared = kinds
        if declared is None:
            declared = getattr(component, "supported_kinds", None)
        if declared is not None:
            for kind in declared:
                self._subject_kinds.add(_component_id(kind, "subject kind"))
        return registered

    def register_driver(self, component_id: str, component: Any) -> Any:
        return self._register(self._drivers, "driver", component_id, component)

    def register_grader(self, component_id: str, component: Any) -> Any:
        return self._register(self._graders, "grader", component_id, component)

    def register_optimizer(self, component_id: str, component: Any) -> Any:
        return self._register(self._optimizers, "optimizer", component_id, component)

    def _get(self, store: Mapping[str, Any], category: str, component_id: str) -> Any:
        try:
            return store[component_id]
        except KeyError as exc:
            raise UnknownComponentError(category, component_id) from exc

    def subject_adapter(self, component_id: str) -> Any:
        return self._get(self._subjects, "subject_adapter", component_id)

    def driver(self, component_id: str) -> Any:
        return self._get(self._drivers, "driver", component_id)

    def grader(self, component_id: str) -> Any:
        return self._get(self._graders, "grader", component_id)

    def optimizer(self, component_id: str) -> Any:
        return self._get(self._optimizers, "optimizer", component_id)

    get_subject_adapter = subject_adapter
    get_driver = driver
    get_grader = grader
    get_optimizer = optimizer

    def component_ids(self, category: Optional[str] = None) -> Mapping[str, Tuple[str, ...]]:
        values = {
            "subject_adapter": tuple(sorted(self._subjects)),
            "driver": tuple(sorted(self._drivers)),
            "grader": tuple(sorted(self._graders)),
            "optimizer": tuple(sorted(self._optimizers)),
        }
        if category is None:
            return values
        if category not in values:
            raise KeyError(category)
        return {category: values[category]}


class EvalPackLoader:
    """Load and freeze a local declaration-only EvalPack."""

    def __init__(self, registry: Optional[ComponentRegistryProtocol] = None) -> None:
        self.registry = registry

    def load(self, ref: Any) -> FrozenEvalPack:
        pack_ref = PackRef.from_value(ref)
        requested = Path(pack_ref.path).expanduser()
        if requested.name == "pack.yaml" and requested.is_file():
            manifest_path = requested
            root_candidate = requested.parent
        else:
            root_candidate = requested
            manifest_path = root_candidate / "pack.yaml"
        try:
            root = root_candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise PackFormatError("Pack root does not exist: {0}".format(root_candidate)) from exc
        if not root.is_dir():
            raise PackFormatError("Pack root is not a directory: {0}".format(root))
        if manifest_path.is_symlink():
            raise PackPathError("Pack manifest cannot be a symlink: {0}".format(manifest_path))
        manifest_path = root / "pack.yaml"
        if not manifest_path.is_file():
            raise PackFormatError("Pack is missing pack.yaml: {0}".format(root))
        _assert_safe_tree(root)
        manifest = _parse_manifest(_read_document(manifest_path))
        _validate_manifest_local_refs(root, manifest)
        resources = _load_manifest_resources(root, manifest)
        scenarios, oracles = self._load_scenarios(root, manifest)
        file_hashes = _hash_tree(root)
        suite_hash = _hash_json(
            {
                "scenarios": scenarios,
                "oracles": oracles,
                "manifest_suite": manifest.suite,
            }
        )
        pack_hash = _hash_json(file_hashes)
        pack = FrozenEvalPack(
            root=root,
            manifest_path=manifest_path,
            manifest=manifest,
            scenarios=tuple(scenarios),
            oracles=oracles,
            resources=resources,
            file_hashes=file_hashes,
            suite_hash=suite_hash,
            pack_hash=pack_hash,
        )
        if self.registry is not None:
            self.validate_or_raise(pack, self.registry)
        return pack

    def load_validated(
        self, ref: Any, registry: ComponentRegistryProtocol
    ) -> FrozenEvalPack:
        pack = self.load(ref)
        self.validate_or_raise(pack, registry)
        return pack

    def _load_scenarios(
        self, root: Path, manifest: EvalPackManifest
    ) -> Tuple[List[FrozenScenario], Dict[str, FrozenOracle]]:
        scenarios: List[FrozenScenario] = []
        oracles: Dict[str, FrozenOracle] = {}
        seen_ids: Set[str] = set()
        suite_refs = list(manifest.suite.local_scenario_refs())
        local_splits = {split for split, _ in suite_refs}
        for split, reference in (
            (ScenarioSplit.VALIDATION.value, manifest.suite.validation_ref),
            (ScenarioSplit.HOLDOUT.value, manifest.suite.holdout_ref),
        ):
            if reference is None or split in local_splits:
                continue
            candidate = root.joinpath(*PurePath(reference).parts)
            if candidate.exists():
                suite_refs.append((split, reference))
            elif PurePath(reference).suffix.lower() in {".json", ".yaml", ".yml"}:
                raise PackPathError(
                    "suite.{0}_ref looks local but does not exist: {1}".format(
                        split, reference
                    )
                )
        for split, reference in suite_refs:
            scenario_path = _safe_pack_path(root, reference, "suite.{0}".format(split), "file")
            document = _read_document(scenario_path)
            for item in _scenario_entries(document, scenario_path):
                _, oracle, _, frozen = _parse_scenario(item, split, root, scenario_path)
                if frozen.id in seen_ids:
                    raise PackFormatError("duplicate scenario id: {0}".format(frozen.id))
                seen_ids.add(frozen.id)
                if oracle is not None and oracle.ref is not None:
                    previous = oracles.get(oracle.ref)
                    if previous is not None and previous.content_hash != oracle.content_hash:
                        raise PackFormatError(
                            "oracle reference resolves to inconsistent content: {0}".format(
                                oracle.ref
                            )
                        )
                    oracles[oracle.ref] = oracle
                scenarios.append(frozen)
        return scenarios, oracles

    def validate(
        self, pack: FrozenEvalPack, registry: ComponentRegistryProtocol
    ) -> PackReport:
        issues: List[PackIssue] = []
        manifest = pack.manifest
        try:
            adapter = _lookup(registry, "subject_adapter", manifest.subject_contract.adapter)
        except UnknownComponentError as exc:
            issues.append(
                PackIssue(
                    "unknown_subject_adapter",
                    str(exc),
                    "manifest.subject_contract.adapter",
                )
            )
            adapter = None
        supported_kinds = set(getattr(registry, "subject_kinds", ()) or ())
        if adapter is not None and not supported_kinds:
            supported_kinds.update(getattr(adapter, "supported_kinds", ()) or ())
        for kind in manifest.subject_contract.kinds:
            if kind not in supported_kinds:
                issues.append(
                    PackIssue(
                        "unknown_subject_kind",
                        "subject kind is not registered: {0}".format(kind),
                        "manifest.subject_contract.kinds",
                    )
                )
        try:
            _lookup(registry, "driver", manifest.driver.type)
        except UnknownComponentError as exc:
            issues.append(PackIssue("unknown_driver", str(exc), "manifest.driver.type"))
        manifest_grader_ids = {grader.id for grader in manifest.graders}
        manifest_graders = {grader.id: grader for grader in manifest.graders}
        for index, grader in enumerate(manifest.graders):
            try:
                _lookup(registry, "grader", grader.type)
            except UnknownComponentError as exc:
                issues.append(
                    PackIssue(
                        "unknown_grader",
                        str(exc),
                        "manifest.graders[{0}].type".format(index),
                    )
                )
        for index, scenario in enumerate(pack.scenarios):
            for grader_id in scenario.grader_ids:
                if grader_id not in manifest_grader_ids:
                    issues.append(
                        PackIssue(
                            "unknown_scenario_grader",
                            "scenario references undeclared grader: {0}".format(grader_id),
                            "scenarios[{0}].grader_ids".format(index),
                        )
                    )
            selected_graders = [
                manifest_graders[grader_id]
                for grader_id in scenario.grader_ids
                if grader_id in manifest_graders
            ]
            if not any(grader.hard for grader in selected_graders):
                issues.append(
                    PackIssue(
                        "scenario_without_hard_grader",
                        "scenario must select at least one hard grader",
                        "scenarios[{0}].grader_ids".format(index),
                    )
                )
            unknown_params = set(scenario.scenario.grader_params).difference(
                scenario.grader_ids
            )
            for grader_id in sorted(unknown_params):
                issues.append(
                    PackIssue(
                        "orphan_grader_params",
                        "scenario params provided for unselected grader: {0}".format(
                            grader_id
                        ),
                        "scenarios[{0}].grader_params".format(index),
                    )
                )
        policy = manifest.optimizer_policy
        if policy is not None:
            try:
                _lookup(registry, "optimizer", policy.adapter)
            except UnknownComponentError as exc:
                issues.append(
                    PackIssue("unknown_optimizer", str(exc), "manifest.optimizer_policy.adapter")
                )
            if any(split != ScenarioSplit.DEV.value for split in policy.visible_splits):
                issues.append(
                    PackIssue(
                        "optimizer_visibility",
                        "optimizer_policy.visible_splits may only include 'dev'",
                        "manifest.optimizer_policy.visible_splits",
                    )
                )
        return PackReport(issues=tuple(issues), pack_hash=pack.pack_hash)

    def validate_or_raise(
        self, pack: FrozenEvalPack, registry: ComponentRegistryProtocol
    ) -> PackReport:
        report = self.validate(pack, registry)
        if not report.ok:
            raise PackValidationError(report)
        return report

    lint = validate


__all__ = [
    "SUPPORTED_API_VERSION",
    "PACK_KIND",
    "PackError",
    "PackFormatError",
    "PackPathError",
    "MissingYamlDependencyError",
    "UnknownComponentError",
    "DuplicateComponentError",
    "UnsupportedApiVersionError",
    "PackValidationError",
    "ComponentRegistry",
    "EvalPackLoader",
]
