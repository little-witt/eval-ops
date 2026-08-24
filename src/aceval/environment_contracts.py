"""Content-addressed contracts for isolated validation environments.

The execution runtime and the validation provider can live on different
machines.  These records bind both sides to immutable Skill, repository,
suite, and environment identities so a branch name or mutable path can never
silently change what was evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple, Union
import urllib.parse

from .contracts import TraceEvent, as_primitive


ENVIRONMENT_BLUEPRINT_API_VERSION = "aceval.environment-blueprint/v1"
CANDIDATE_BUNDLE_API_VERSION = "aceval.candidate-bundle/v1"
VALIDATION_REQUEST_API_VERSION = "aceval.validation-request/v1"
VALIDATION_RECEIPT_API_VERSION = "aceval.validation-receipt/v1"
RUN_ENVELOPE_API_VERSION = "aceval.run-envelope/v1"

MAX_CONTRACT_BYTES = 2 * 1024 * 1024
MAX_TREE_FILES = 20_000
MAX_TREE_BYTES = 512 * 1024 * 1024


class EnvironmentContractError(ValueError):
    """A validation contract is malformed, mutable, or internally inconsistent."""


class ValidationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    CANDIDATE_FAILED = "candidate_failed"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"
    CANCELLED = "cancelled"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _strict_object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EnvironmentContractError("%s must be a JSON object" % label)
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: Iterable[str], label: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise EnvironmentContractError(
            "%s contains unsupported fields: %s" % (label, ", ".join(unknown))
        )


def _required_text(value: Any, label: str, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise EnvironmentContractError("%s must be a trimmed non-empty safe string" % label)
    return value


def _optional_text(value: Any, label: str, maximum: int = 2048) -> Optional[str]:
    if value is None:
        return None
    return _required_text(value, label, maximum)


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EnvironmentContractError("%s must be a positive integer" % label)
    return value


def _positive_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise EnvironmentContractError("%s must be a positive finite number" % label)
    return float(value)


def normalize_sha256(value: Any, label: str = "sha256") -> str:
    text = _required_text(value, label, 256)
    if text.startswith("sha256:"):
        digest = text[7:]
    else:
        digest = text
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise EnvironmentContractError("%s must be a lowercase SHA-256 digest" % label)
    return "sha256:" + digest


def canonical_hash(value: Any) -> str:
    try:
        payload = json.dumps(
            as_primitive(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise EnvironmentContractError("contract must be strict JSON") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EnvironmentContractError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_strict_json(path: Union[str, Path], label: str) -> Mapping[str, Any]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise EnvironmentContractError("%s must be a regular file" % label)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise EnvironmentContractError("cannot read %s" % label) from exc
    if len(raw) > MAX_CONTRACT_BYTES:
        raise EnvironmentContractError("%s exceeds byte limit" % label)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(
                EnvironmentContractError("non-standard JSON constant: %s" % item)
            ),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EnvironmentContractError("%s is not strict UTF-8 JSON" % label) from exc
    return _strict_object(value, label)


def write_contract(path: Union[str, Path], value: Any, *, force: bool = False) -> Path:
    target = Path(path).expanduser().absolute()
    if target.is_symlink():
        raise EnvironmentContractError("contract output cannot be a symlink")
    if target.exists() and not force:
        raise EnvironmentContractError("contract output already exists: %s" % target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            as_primitive(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _strings(value: Any, label: str, *, allow_empty: bool = True) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EnvironmentContractError("%s must be an array" % label)
    result = tuple(_required_text(item, "%s[]" % label) for item in value)
    if not allow_empty and not result:
        raise EnvironmentContractError("%s must not be empty" % label)
    if len(set(result)) != len(result):
        raise EnvironmentContractError("%s must not contain duplicates" % label)
    return result


def _safe_relative(value: Any, label: str) -> str:
    text = _required_text(value, label)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or text in (".", ""):
        raise EnvironmentContractError("%s must be a safe relative path" % label)
    return path.as_posix()


@dataclass(frozen=True)
class CommandSpec:
    argv: Tuple[str, ...]
    cwd: str = "/workspace"
    timeout_seconds: float = 120.0
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        argv = tuple(_required_text(item, "command.argv[]", 4096) for item in self.argv)
        if not argv:
            raise EnvironmentContractError("command.argv must not be empty")
        object.__setattr__(self, "argv", argv)
        cwd = _required_text(self.cwd, "command.cwd")
        if not cwd.startswith("/") or ".." in Path(cwd).parts:
            raise EnvironmentContractError("command.cwd must be an absolute container path")
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(
            self, "timeout_seconds", _positive_number(self.timeout_seconds, "command.timeout_seconds")
        )
        env = _strict_object(self.env, "command.env")
        normalized = {}
        for key, value in env.items():
            name = _required_text(key, "command.env key", 128)
            if not name.replace("_", "A").isalnum() or not name[0].isalpha():
                raise EnvironmentContractError("command.env contains an invalid name")
            normalized[name] = _required_text(value, "command.env value", 4096)
        object.__setattr__(self, "env", _freeze(normalized))

    @classmethod
    def from_mapping(cls, value: Any) -> "CommandSpec":
        mapping = _strict_object(value, "command")
        _reject_unknown(mapping, ("argv", "cwd", "timeout_seconds", "env"), "command")
        argv = mapping.get("argv")
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise EnvironmentContractError("command.argv must be an array")
        return cls(
            argv=tuple(argv),
            cwd=mapping.get("cwd", "/workspace"),
            timeout_seconds=mapping.get("timeout_seconds", 120.0),
            env=mapping.get("env", {}),
        )


@dataclass(frozen=True)
class EnvironmentLimits:
    cpus: float = 2.0
    memory_mb: int = 4096
    pids: int = 256
    timeout_seconds: float = 900.0
    tmpfs_mb: int = 512

    def __post_init__(self) -> None:
        object.__setattr__(self, "cpus", _positive_number(self.cpus, "limits.cpus"))
        for name in ("memory_mb", "pids", "tmpfs_mb"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), "limits.%s" % name))
        object.__setattr__(
            self, "timeout_seconds", _positive_number(self.timeout_seconds, "limits.timeout_seconds")
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "EnvironmentLimits":
        mapping = _strict_object(value, "limits")
        _reject_unknown(
            mapping,
            ("cpus", "memory_mb", "pids", "timeout_seconds", "tmpfs_mb"),
            "limits",
        )
        return cls(**mapping)


def _pinned_image(value: Any) -> str:
    text = _required_text(value, "image", 1024)
    digest = text.split("@", 1)[1] if "@" in text else text
    normalize_sha256(digest, "image digest")
    return text


@dataclass(frozen=True)
class EnvironmentBlueprint:
    api_version: str
    id: str
    image: str
    capabilities: Tuple[str, ...]
    provider: str = "local-docker"
    network: str = "none"
    limits: EnvironmentLimits = field(default_factory=EnvironmentLimits)
    health_checks: Tuple[CommandSpec, ...] = ()
    validation_commands: Tuple[CommandSpec, ...] = ()
    output_paths: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    blueprint_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if self.api_version != ENVIRONMENT_BLUEPRINT_API_VERSION:
            raise EnvironmentContractError("unsupported EnvironmentBlueprint api_version")
        object.__setattr__(self, "id", _required_text(self.id, "id"))
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(self, "image", _pinned_image(self.image))
        object.__setattr__(
            self, "capabilities", _strings(self.capabilities, "capabilities", allow_empty=False)
        )
        if self.network != "none":
            raise EnvironmentContractError("EnvironmentBlueprint v1 only permits network=none")
        if not isinstance(self.limits, EnvironmentLimits):
            object.__setattr__(self, "limits", EnvironmentLimits.from_mapping(self.limits))
        health = tuple(
            item if isinstance(item, CommandSpec) else CommandSpec.from_mapping(item)
            for item in self.health_checks
        )
        commands = tuple(
            item if isinstance(item, CommandSpec) else CommandSpec.from_mapping(item)
            for item in self.validation_commands
        )
        if not commands:
            raise EnvironmentContractError("validation_commands must not be empty")
        object.__setattr__(self, "health_checks", health)
        object.__setattr__(self, "validation_commands", commands)
        object.__setattr__(
            self,
            "output_paths",
            tuple(_safe_relative(item, "output_paths[]") for item in self.output_paths),
        )
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        payload = {
            "api_version": self.api_version,
            "id": self.id,
            "image": self.image,
            "capabilities": self.capabilities,
            "provider": self.provider,
            "network": self.network,
            "limits": self.limits,
            "health_checks": health,
            "validation_commands": commands,
            "output_paths": self.output_paths,
            "metadata": self.metadata,
        }
        computed = canonical_hash(payload)
        if self.blueprint_hash is not None and normalize_sha256(
            self.blueprint_hash, "blueprint_hash"
        ) != computed:
            raise EnvironmentContractError("blueprint_hash does not match contents")
        object.__setattr__(self, "blueprint_hash", computed)

    @classmethod
    def from_mapping(cls, value: Any) -> "EnvironmentBlueprint":
        mapping = _strict_object(value, "EnvironmentBlueprint")
        _reject_unknown(
            mapping,
            (
                "api_version", "id", "image", "capabilities", "provider", "network",
                "limits", "health_checks", "validation_commands", "output_paths",
                "metadata", "blueprint_hash",
            ),
            "EnvironmentBlueprint",
        )
        return cls(
            api_version=mapping.get("api_version"),
            id=mapping.get("id"),
            image=mapping.get("image"),
            capabilities=_strings(mapping.get("capabilities", ()), "capabilities", allow_empty=False),
            provider=mapping.get("provider", "local-docker"),
            network=mapping.get("network", "none"),
            limits=EnvironmentLimits.from_mapping(mapping.get("limits", {})),
            health_checks=tuple(
                CommandSpec.from_mapping(item) for item in mapping.get("health_checks", ())
            ),
            validation_commands=tuple(
                CommandSpec.from_mapping(item)
                for item in mapping.get("validation_commands", ())
            ),
            output_paths=_strings(mapping.get("output_paths", ()), "output_paths"),
            metadata=mapping.get("metadata", {}),
            blueprint_hash=mapping.get("blueprint_hash"),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "EnvironmentBlueprint":
        return cls.from_mapping(load_strict_json(path, "EnvironmentBlueprint"))


@dataclass(frozen=True)
class RepositoryIdentity:
    base_commit: Optional[str] = None
    result_commit: Optional[str] = None
    tree_hash: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("base_commit", "result_commit"):
            value = getattr(self, name)
            if value is not None:
                text = _required_text(value, "repository.%s" % name, 128)
                if len(text) < 7 or any(character not in "0123456789abcdef" for character in text):
                    raise EnvironmentContractError("repository.%s must be a lowercase git object id" % name)
                object.__setattr__(self, name, text)
        if self.tree_hash is not None:
            object.__setattr__(self, "tree_hash", normalize_sha256(self.tree_hash, "tree_hash"))

    @classmethod
    def from_mapping(cls, value: Any) -> "RepositoryIdentity":
        mapping = _strict_object(value, "repository")
        _reject_unknown(mapping, ("base_commit", "result_commit", "tree_hash"), "repository")
        return cls(**mapping)


@dataclass(frozen=True)
class CandidateBundle:
    api_version: str
    subject_hash: str
    artifact_uri: str
    artifact_sha256: str
    producer_run_id: str
    repository: RepositoryIdentity = field(default_factory=RepositoryIdentity)
    parent_subject_hash: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    bundle_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if self.api_version != CANDIDATE_BUNDLE_API_VERSION:
            raise EnvironmentContractError("unsupported CandidateBundle api_version")
        object.__setattr__(self, "subject_hash", normalize_sha256(self.subject_hash, "subject_hash"))
        if self.parent_subject_hash is not None:
            object.__setattr__(
                self,
                "parent_subject_hash",
                normalize_sha256(self.parent_subject_hash, "parent_subject_hash"),
            )
        uri = _required_text(self.artifact_uri, "artifact_uri", 8192)
        parsed = urllib.parse.urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise EnvironmentContractError("CandidateBundle v1 requires a local file:// artifact_uri")
        if not urllib.parse.unquote(parsed.path).startswith("/"):
            raise EnvironmentContractError("artifact_uri must contain an absolute path")
        object.__setattr__(self, "artifact_uri", uri)
        object.__setattr__(
            self, "artifact_sha256", normalize_sha256(self.artifact_sha256, "artifact_sha256")
        )
        object.__setattr__(self, "producer_run_id", _required_text(self.producer_run_id, "producer_run_id"))
        if not isinstance(self.repository, RepositoryIdentity):
            object.__setattr__(self, "repository", RepositoryIdentity.from_mapping(self.repository))
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        payload = {
            "api_version": self.api_version,
            "subject_hash": self.subject_hash,
            "parent_subject_hash": self.parent_subject_hash,
            "artifact_uri": self.artifact_uri,
            "artifact_sha256": self.artifact_sha256,
            "producer_run_id": self.producer_run_id,
            "repository": self.repository,
            "metadata": self.metadata,
        }
        computed = canonical_hash(payload)
        if self.bundle_hash is not None and normalize_sha256(self.bundle_hash, "bundle_hash") != computed:
            raise EnvironmentContractError("bundle_hash does not match contents")
        object.__setattr__(self, "bundle_hash", computed)

    @property
    def artifact_path(self) -> Path:
        parsed = urllib.parse.urlsplit(self.artifact_uri)
        return Path(urllib.parse.unquote(parsed.path)).resolve()

    @classmethod
    def from_mapping(cls, value: Any) -> "CandidateBundle":
        mapping = _strict_object(value, "CandidateBundle")
        _reject_unknown(
            mapping,
            (
                "api_version", "subject_hash", "parent_subject_hash", "artifact_uri",
                "artifact_sha256", "producer_run_id", "repository", "metadata", "bundle_hash",
            ),
            "CandidateBundle",
        )
        return cls(
            api_version=mapping.get("api_version"),
            subject_hash=mapping.get("subject_hash"),
            parent_subject_hash=mapping.get("parent_subject_hash"),
            artifact_uri=mapping.get("artifact_uri"),
            artifact_sha256=mapping.get("artifact_sha256"),
            producer_run_id=mapping.get("producer_run_id"),
            repository=RepositoryIdentity.from_mapping(mapping.get("repository", {})),
            metadata=mapping.get("metadata", {}),
            bundle_hash=mapping.get("bundle_hash"),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "CandidateBundle":
        return cls.from_mapping(load_strict_json(path, "CandidateBundle"))


def directory_tree_hash(
    root: Union[str, Path],
    *,
    max_files: int = MAX_TREE_FILES,
    max_total_bytes: int = MAX_TREE_BYTES,
) -> str:
    source = Path(root).expanduser().resolve()
    if source.is_symlink() or not source.is_dir():
        raise EnvironmentContractError("candidate artifact must be a regular directory")
    digest = hashlib.sha256()
    count = 0
    total = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise EnvironmentContractError("candidate artifact contains a symlink: %s" % relative)
        if path.is_dir():
            continue
        if not path.is_file():
            raise EnvironmentContractError("candidate artifact contains a non-regular file")
        count += 1
        if count > max_files:
            raise EnvironmentContractError("candidate artifact exceeds file-count limit")
        size = path.stat().st_size
        total += size
        if total > max_total_bytes:
            raise EnvironmentContractError("candidate artifact exceeds byte limit")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\x00")
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(b"\x00")
    return "sha256:" + digest.hexdigest()


def create_candidate_bundle(
    artifact_path: Union[str, Path],
    *,
    subject_hash: str,
    producer_run_id: str,
    parent_subject_hash: Optional[str] = None,
    base_commit: Optional[str] = None,
    result_commit: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> CandidateBundle:
    path = Path(artifact_path).expanduser().resolve()
    tree_hash = directory_tree_hash(path)
    return CandidateBundle(
        api_version=CANDIDATE_BUNDLE_API_VERSION,
        subject_hash=subject_hash,
        parent_subject_hash=parent_subject_hash,
        artifact_uri=path.as_uri(),
        artifact_sha256=tree_hash,
        producer_run_id=producer_run_id,
        repository=RepositoryIdentity(
            base_commit=base_commit,
            result_commit=result_commit,
            tree_hash=tree_hash,
        ),
        metadata=dict(metadata or {}),
    )


def verify_candidate_bundle(bundle: CandidateBundle) -> Path:
    path = bundle.artifact_path
    actual = directory_tree_hash(path)
    if actual != bundle.artifact_sha256:
        raise EnvironmentContractError("candidate artifact content does not match artifact_sha256")
    if bundle.repository.tree_hash is not None and actual != bundle.repository.tree_hash:
        raise EnvironmentContractError("candidate artifact content does not match repository.tree_hash")
    return path


@dataclass(frozen=True)
class ValidationRequest:
    api_version: str
    run_id: str
    suite_hash: str
    scenario_id: str
    candidate: CandidateBundle
    blueprint: EnvironmentBlueprint
    grader_contract: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    request_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if self.api_version != VALIDATION_REQUEST_API_VERSION:
            raise EnvironmentContractError("unsupported ValidationRequest api_version")
        object.__setattr__(self, "run_id", _required_text(self.run_id, "run_id"))
        object.__setattr__(self, "suite_hash", normalize_sha256(self.suite_hash, "suite_hash"))
        object.__setattr__(self, "scenario_id", _required_text(self.scenario_id, "scenario_id"))
        if not isinstance(self.candidate, CandidateBundle):
            object.__setattr__(self, "candidate", CandidateBundle.from_mapping(self.candidate))
        if not isinstance(self.blueprint, EnvironmentBlueprint):
            object.__setattr__(self, "blueprint", EnvironmentBlueprint.from_mapping(self.blueprint))
        object.__setattr__(
            self, "grader_contract", _required_text(self.grader_contract, "grader_contract")
        )
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        payload = {
            "api_version": self.api_version,
            "run_id": self.run_id,
            "suite_hash": self.suite_hash,
            "scenario_id": self.scenario_id,
            "candidate": self.candidate,
            "blueprint": self.blueprint,
            "grader_contract": self.grader_contract,
            "metadata": self.metadata,
        }
        computed = canonical_hash(payload)
        if self.request_hash is not None and normalize_sha256(
            self.request_hash, "request_hash"
        ) != computed:
            raise EnvironmentContractError("request_hash does not match contents")
        object.__setattr__(self, "request_hash", computed)

    @classmethod
    def from_mapping(cls, value: Any) -> "ValidationRequest":
        mapping = _strict_object(value, "ValidationRequest")
        _reject_unknown(
            mapping,
            (
                "api_version", "run_id", "suite_hash", "scenario_id", "candidate",
                "blueprint", "grader_contract", "metadata", "request_hash",
            ),
            "ValidationRequest",
        )
        return cls(
            api_version=mapping.get("api_version"),
            run_id=mapping.get("run_id"),
            suite_hash=mapping.get("suite_hash"),
            scenario_id=mapping.get("scenario_id"),
            candidate=CandidateBundle.from_mapping(mapping.get("candidate")),
            blueprint=EnvironmentBlueprint.from_mapping(mapping.get("blueprint")),
            grader_contract=mapping.get("grader_contract"),
            metadata=mapping.get("metadata", {}),
            request_hash=mapping.get("request_hash"),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "ValidationRequest":
        return cls.from_mapping(load_strict_json(path, "ValidationRequest"))


@dataclass(frozen=True)
class CommandReceipt:
    argv: Tuple[str, ...]
    cwd: str
    exit_code: Optional[int]
    duration_ms: float
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "argv",
            tuple(_required_text(item, "command receipt argv[]", 4096) for item in self.argv),
        )
        object.__setattr__(self, "cwd", _required_text(self.cwd, "command receipt cwd"))
        if self.exit_code is not None and (
            not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool)
        ):
            raise EnvironmentContractError("command receipt exit_code must be an integer")
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, (int, float))
            or not math.isfinite(float(self.duration_ms))
            or self.duration_ms < 0
        ):
            raise EnvironmentContractError("command receipt duration_ms is invalid")
        for name in ("stdout", "stderr"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise EnvironmentContractError("command receipt %s must be text" % name)
        if not isinstance(self.timed_out, bool):
            raise EnvironmentContractError("command receipt timed_out must be a boolean")

    @classmethod
    def from_mapping(cls, value: Any) -> "CommandReceipt":
        mapping = _strict_object(value, "CommandReceipt")
        _reject_unknown(
            mapping,
            ("argv", "cwd", "exit_code", "duration_ms", "stdout", "stderr", "timed_out"),
            "CommandReceipt",
        )
        argv = mapping.get("argv")
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise EnvironmentContractError("CommandReceipt.argv must be an array")
        return cls(
            argv=tuple(argv),
            cwd=mapping.get("cwd"),
            exit_code=mapping.get("exit_code"),
            duration_ms=mapping.get("duration_ms"),
            stdout=mapping.get("stdout", ""),
            stderr=mapping.get("stderr", ""),
            timed_out=mapping.get("timed_out", False),
        )


@dataclass(frozen=True)
class ValidationReceipt:
    api_version: str
    request_hash: str
    provider: str
    status: ValidationStatus
    environment_fingerprint: str
    commands: Tuple[CommandReceipt, ...] = ()
    artifact_hashes: Mapping[str, str] = field(default_factory=dict)
    infrastructure_errors: Tuple[str, ...] = ()
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    receipt_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if self.api_version != VALIDATION_RECEIPT_API_VERSION:
            raise EnvironmentContractError("unsupported ValidationReceipt api_version")
        object.__setattr__(self, "request_hash", normalize_sha256(self.request_hash, "request_hash"))
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        if not isinstance(self.status, ValidationStatus):
            object.__setattr__(self, "status", ValidationStatus(self.status))
        object.__setattr__(
            self,
            "environment_fingerprint",
            normalize_sha256(self.environment_fingerprint, "environment_fingerprint"),
        )
        object.__setattr__(self, "commands", tuple(self.commands))
        artifact_hashes = {
            _safe_relative(path, "artifact_hashes path"): normalize_sha256(value, "artifact hash")
            for path, value in self.artifact_hashes.items()
        }
        object.__setattr__(self, "artifact_hashes", _freeze(artifact_hashes))
        object.__setattr__(
            self,
            "infrastructure_errors",
            tuple(_required_text(item, "infrastructure_errors[]", 8192) for item in self.infrastructure_errors),
        )
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        payload = {
            "api_version": self.api_version,
            "request_hash": self.request_hash,
            "provider": self.provider,
            "status": self.status,
            "environment_fingerprint": self.environment_fingerprint,
            "commands": self.commands,
            "artifact_hashes": self.artifact_hashes,
            "infrastructure_errors": self.infrastructure_errors,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": self.metadata,
        }
        computed = canonical_hash(payload)
        if self.receipt_hash is not None and normalize_sha256(
            self.receipt_hash, "receipt_hash"
        ) != computed:
            raise EnvironmentContractError("receipt_hash does not match contents")
        object.__setattr__(self, "receipt_hash", computed)

    @classmethod
    def from_mapping(cls, value: Any) -> "ValidationReceipt":
        mapping = _strict_object(value, "ValidationReceipt")
        _reject_unknown(
            mapping,
            (
                "api_version", "request_hash", "provider", "status",
                "environment_fingerprint", "commands", "artifact_hashes",
                "infrastructure_errors", "started_at", "finished_at", "metadata",
                "receipt_hash",
            ),
            "ValidationReceipt",
        )
        raw_commands = mapping.get("commands", ())
        if isinstance(raw_commands, (str, bytes)) or not isinstance(raw_commands, Sequence):
            raise EnvironmentContractError("ValidationReceipt.commands must be an array")
        return cls(
            api_version=mapping.get("api_version"),
            request_hash=mapping.get("request_hash"),
            provider=mapping.get("provider"),
            status=mapping.get("status"),
            environment_fingerprint=mapping.get("environment_fingerprint"),
            commands=tuple(CommandReceipt.from_mapping(item) for item in raw_commands),
            artifact_hashes=mapping.get("artifact_hashes", {}),
            infrastructure_errors=_strings(
                mapping.get("infrastructure_errors", ()), "infrastructure_errors"
            ),
            started_at=_optional_text(mapping.get("started_at"), "started_at"),
            finished_at=_optional_text(mapping.get("finished_at"), "finished_at"),
            metadata=mapping.get("metadata", {}),
            receipt_hash=mapping.get("receipt_hash"),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "ValidationReceipt":
        return cls.from_mapping(load_strict_json(path, "ValidationReceipt"))


@dataclass(frozen=True)
class RunEnvelope:
    api_version: str
    run_id: str
    subject_hash: str
    suite_hash: str
    scenario_id: str
    runtime_profile_hash: str
    output: Any = None
    trace: Tuple[TraceEvent, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    candidate_bundle_hash: Optional[str] = None
    validation_receipt_hashes: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    envelope_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if self.api_version != RUN_ENVELOPE_API_VERSION:
            raise EnvironmentContractError("unsupported RunEnvelope api_version")
        object.__setattr__(self, "run_id", _required_text(self.run_id, "run_id"))
        object.__setattr__(self, "subject_hash", normalize_sha256(self.subject_hash, "subject_hash"))
        object.__setattr__(self, "suite_hash", normalize_sha256(self.suite_hash, "suite_hash"))
        object.__setattr__(self, "scenario_id", _required_text(self.scenario_id, "scenario_id"))
        object.__setattr__(
            self,
            "runtime_profile_hash",
            normalize_sha256(self.runtime_profile_hash, "runtime_profile_hash"),
        )
        if self.candidate_bundle_hash is not None:
            object.__setattr__(
                self,
                "candidate_bundle_hash",
                normalize_sha256(self.candidate_bundle_hash, "candidate_bundle_hash"),
            )
        object.__setattr__(
            self,
            "validation_receipt_hashes",
            tuple(normalize_sha256(item, "validation receipt hash") for item in self.validation_receipt_hashes),
        )
        object.__setattr__(self, "trace", tuple(self.trace))
        object.__setattr__(self, "usage", _freeze(self.usage))
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        payload = {
            "api_version": self.api_version,
            "run_id": self.run_id,
            "subject_hash": self.subject_hash,
            "suite_hash": self.suite_hash,
            "scenario_id": self.scenario_id,
            "runtime_profile_hash": self.runtime_profile_hash,
            "output": self.output,
            "trace": self.trace,
            "usage": self.usage,
            "error": self.error,
            "candidate_bundle_hash": self.candidate_bundle_hash,
            "validation_receipt_hashes": self.validation_receipt_hashes,
            "metadata": self.metadata,
        }
        computed = canonical_hash(payload)
        if self.envelope_hash is not None and normalize_sha256(
            self.envelope_hash, "envelope_hash"
        ) != computed:
            raise EnvironmentContractError("envelope_hash does not match contents")
        object.__setattr__(self, "envelope_hash", computed)


__all__ = [
    "CANDIDATE_BUNDLE_API_VERSION",
    "CandidateBundle",
    "CommandReceipt",
    "CommandSpec",
    "ENVIRONMENT_BLUEPRINT_API_VERSION",
    "EnvironmentBlueprint",
    "EnvironmentContractError",
    "EnvironmentLimits",
    "RUN_ENVELOPE_API_VERSION",
    "RepositoryIdentity",
    "RunEnvelope",
    "VALIDATION_RECEIPT_API_VERSION",
    "VALIDATION_REQUEST_API_VERSION",
    "ValidationReceipt",
    "ValidationRequest",
    "ValidationStatus",
    "canonical_hash",
    "create_candidate_bundle",
    "directory_tree_hash",
    "load_strict_json",
    "normalize_sha256",
    "verify_candidate_bundle",
    "write_contract",
]
