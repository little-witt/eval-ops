from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    FrozenSet,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)


class ScenarioSplit(str, Enum):
    DEV = "dev"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class GradeStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUABLE = "not_evaluable"
    ERROR = "error"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class CandidateStatus(str, Enum):
    PROPOSED = "proposed"
    VALID = "valid"
    REJECTED = "rejected"
    FROZEN = "frozen"
    ACCEPTED = "accepted"


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


CANDIDATE_PATCH_OPTIMIZER_CONTRACT = "aceval.optimizer/candidate-patch-v1"


def _deep_freeze(value: Any, active: Optional[set] = None) -> Any:
    if isinstance(value, Mapping):
        seen = active if active is not None else set()
        identity = id(value)
        if identity in seen:
            raise ValueError("cyclic mappings are not supported in frozen contracts")
        seen.add(identity)
        try:
            return MappingProxyType(
                {
                    _deep_freeze(key, seen): _deep_freeze(item, seen)
                    for key, item in value.items()
                }
            )
        finally:
            seen.remove(identity)
    if isinstance(value, (list, tuple)):
        seen = active if active is not None else set()
        identity = id(value)
        if identity in seen:
            raise ValueError("cyclic sequences are not supported in frozen contracts")
        seen.add(identity)
        try:
            return tuple(_deep_freeze(item, seen) for item in value)
        finally:
            seen.remove(identity)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item, active) for item in value)
    return value


@dataclass(frozen=True)
class PackRef:
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))

    @classmethod
    def from_value(cls, value: Any) -> "PackRef":
        if isinstance(value, cls):
            return value
        return cls(path=Path(value))


@dataclass(frozen=True)
class PackMetadata:
    name: str
    version: str
    description: str = ""
    labels: Mapping[str, str] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "labels", _deep_freeze(self.labels))
        object.__setattr__(self, "extra", _deep_freeze(self.extra))


@dataclass(frozen=True)
class SubjectContract:
    kinds: Tuple[str, ...]
    adapter: str
    entrypoint: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kinds", tuple(self.kinds))
        object.__setattr__(self, "params", _deep_freeze(self.params))


@dataclass(frozen=True)
class DriverSpec:
    type: str
    required_runtime_capabilities: Tuple[str, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_runtime_capabilities",
            tuple(self.required_runtime_capabilities),
        )
        object.__setattr__(self, "params", _deep_freeze(self.params))


@dataclass(frozen=True)
class SuiteSpec:
    dev: Optional[str] = None
    validation: Optional[str] = None
    holdout: Optional[str] = None
    validation_ref: Optional[str] = None
    holdout_ref: Optional[str] = None

    def local_scenario_refs(self) -> Tuple[Tuple[str, str], ...]:
        refs = []
        for split, value in (
            (ScenarioSplit.DEV.value, self.dev),
            (ScenarioSplit.VALIDATION.value, self.validation),
            (ScenarioSplit.HOLDOUT.value, self.holdout),
        ):
            if value is not None:
                refs.append((split, value))
        return tuple(refs)


@dataclass(frozen=True)
class GraderSpec:
    id: str
    type: str
    hard: bool = True
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", _deep_freeze(self.params))


@dataclass(frozen=True)
class OptimizerPolicySpec:
    adapter: str
    patchable_components: Tuple[str, ...] = ()
    allowed_paths: Tuple[str, ...] = ()
    visible_splits: Tuple[str, ...] = (ScenarioSplit.DEV.value,)
    beam_width: int = 1
    max_rounds: int = 1
    max_candidate_snapshots: int = 1
    max_added_lines: Optional[int] = None
    forbid_case_literals: bool = True
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("patchable_components", "allowed_paths", "visible_splits"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "params", _deep_freeze(self.params))


@dataclass(frozen=True)
class EvalPackManifest:
    api_version: str
    kind: str
    metadata: PackMetadata
    subject_contract: SubjectContract
    driver: DriverSpec
    suite: SuiteSpec
    graders: Tuple[GraderSpec, ...]
    optimizer_policy: Optional[OptimizerPolicySpec] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "graders", tuple(self.graders))

    def grader_by_id(self, grader_id: str) -> GraderSpec:
        for grader in self.graders:
            if grader.id == grader_id:
                return grader
        raise KeyError(grader_id)


@dataclass(frozen=True)
class Scenario:
    id: str
    split: str = ScenarioSplit.DEV.value
    prompt: str = ""
    fixtures: Tuple[str, ...] = ()
    oracle_ref: Optional[str] = None
    grader_ids: Tuple[str, ...] = ()
    grader_params: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    timeout_seconds: int = 120
    tags: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixtures", tuple(self.fixtures))
        object.__setattr__(self, "grader_ids", tuple(self.grader_ids))
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "grader_params", _deep_freeze(self.grader_params))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class Oracle:
    data: Any = None
    ref: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _deep_freeze(self.data))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class FrozenOracle:
    data: Any = None
    ref: Optional[str] = None
    content_hash: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _deep_freeze(self.data))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class FrozenScenario:
    scenario: Scenario
    oracle: Optional[FrozenOracle] = None
    fixture_hashes: Mapping[str, str] = field(default_factory=dict)
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixture_hashes", _deep_freeze(self.fixture_hashes))

    @property
    def id(self) -> str:
        return self.scenario.id

    @property
    def split(self) -> str:
        return self.scenario.split

    @property
    def prompt(self) -> str:
        return self.scenario.prompt

    @property
    def fixtures(self) -> Tuple[str, ...]:
        return self.scenario.fixtures

    @property
    def grader_ids(self) -> Tuple[str, ...]:
        return self.scenario.grader_ids

    @property
    def timeout_seconds(self) -> int:
        return self.scenario.timeout_seconds


@dataclass(frozen=True)
class DriverScenarioView:
    id: str
    split: str
    prompt: str
    fixtures: Tuple[str, ...] = ()
    timeout_seconds: int = 120
    tags: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixtures", tuple(self.fixtures))
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class FrozenEvalPack:
    root: Path
    manifest_path: Path
    manifest: EvalPackManifest
    scenarios: Tuple[FrozenScenario, ...]
    oracles: Mapping[str, FrozenOracle]
    resources: Mapping[str, Any]
    file_hashes: Mapping[str, str]
    suite_hash: str
    pack_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "manifest_path", Path(self.manifest_path))
        object.__setattr__(self, "scenarios", tuple(self.scenarios))
        object.__setattr__(self, "oracles", _deep_freeze(self.oracles))
        object.__setattr__(self, "resources", _deep_freeze(self.resources))
        object.__setattr__(self, "file_hashes", _deep_freeze(self.file_hashes))

    def scenario_by_id(self, scenario_id: str) -> FrozenScenario:
        for scenario in self.scenarios:
            if scenario.id == scenario_id:
                return scenario
        raise KeyError(scenario_id)


@dataclass(frozen=True)
class PackIssue:
    code: str
    message: str
    path: str = ""
    severity: IssueSeverity = IssueSeverity.ERROR


@dataclass(frozen=True)
class PackReport:
    issues: Tuple[PackIssue, ...] = ()
    pack_hash: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not any(issue.severity == IssueSeverity.ERROR for issue in self.issues)

    @property
    def errors(self) -> Tuple[PackIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity == IssueSeverity.ERROR
        )

    @property
    def warnings(self) -> Tuple[PackIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity == IssueSeverity.WARNING
        )


@dataclass(frozen=True)
class RuntimeCapabilities:
    values: FrozenSet[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", frozenset(self.values))

    @classmethod
    def from_values(cls, values: Iterable[str]) -> "RuntimeCapabilities":
        return cls(values=frozenset(values))

    def supports(self, capability: str) -> bool:
        return capability in self.values

    def missing(self, required: Iterable[str]) -> FrozenSet[str]:
        return frozenset(required).difference(self.values)


@dataclass(frozen=True)
class RuntimeProfile:
    adapter: str
    model: Optional[str] = None
    capabilities: RuntimeCapabilities = field(default_factory=RuntimeCapabilities)
    parameters: Mapping[str, Any] = field(default_factory=dict)
    tool_policy: Mapping[str, Any] = field(default_factory=dict)
    environment_hash: Optional[str] = None
    profile_hash: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, RuntimeCapabilities):
            object.__setattr__(
                self,
                "capabilities",
                RuntimeCapabilities.from_values(self.capabilities),
            )
        object.__setattr__(self, "parameters", _deep_freeze(self.parameters))
        object.__setattr__(self, "tool_policy", _deep_freeze(self.tool_policy))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))
        payload = {
            "adapter": self.adapter,
            "model": self.model,
            "capabilities": sorted(self.capabilities.values),
            "parameters": self.parameters,
            "tool_policy": self.tool_policy,
            "environment_hash": self.environment_hash,
            "metadata": self.metadata,
        }
        try:
            encoded = json.dumps(
                as_primitive(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("RuntimeProfile must be JSON serializable") from exc
        computed = hashlib.sha256(encoded).hexdigest()
        if self.profile_hash is not None and self.profile_hash != computed:
            raise ValueError("RuntimeProfile profile_hash does not match its contents")
        object.__setattr__(self, "profile_hash", computed)


@dataclass(frozen=True)
class RunBudget:
    max_wall_time_seconds: Optional[float] = None
    max_total_tokens: Optional[int] = None
    max_cost_usd: Optional[float] = None
    max_tool_calls: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("max_total_tokens", "max_tool_calls"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
            ):
                raise ValueError("%s must be a positive integer" % name)
        for name in ("max_wall_time_seconds", "max_cost_usd"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError("%s must be a positive finite number" % name)


Budget = RunBudget


@dataclass(frozen=True)
class SubjectSnapshot:
    kind: str
    uri: str
    content_hash: str
    parent_hash: Optional[str] = None
    content: Optional[Any] = None
    files: Mapping[str, bytes] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", _deep_freeze(self.files))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class TraceEvent:
    kind: str
    seq: int = 0
    timestamp: Optional[str] = None
    name: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    tool: Optional[str] = None
    duration_ms: Optional[float] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _deep_freeze(self.payload))

    @property
    def type(self) -> str:
        return self.kind


def canonical_trace_kind(value: Any) -> str:
    if isinstance(value, Mapping):
        raw = value.get("kind") or value.get("type") or ""
    else:
        raw = getattr(value, "kind", None) or getattr(value, "type", None) or ""
    kind = str(raw)
    return "tool_call" if kind in ("tool_call", "tool", "tool_start") else kind


def is_tool_call_event(value: Any) -> bool:
    return canonical_trace_kind(value) == "tool_call"


@dataclass(frozen=True)
class PreparedScenario:
    workspace: Optional[Path] = None
    prompt: str = ""
    baseline_files: Mapping[str, Any] = field(default_factory=dict)
    artifact_paths: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.workspace is not None:
            object.__setattr__(self, "workspace", Path(self.workspace))
        object.__setattr__(self, "baseline_files", _deep_freeze(self.baseline_files))
        object.__setattr__(self, "artifact_paths", tuple(self.artifact_paths))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class RunContext:
    run_id: str
    attempt: int = 1
    pack_hash: Optional[str] = None
    subject_hash: Optional[str] = None
    runtime_profile: Optional[RuntimeProfile] = None
    budget: Optional[RunBudget] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class RuntimeResult:
    final_output: Any = None
    trace: Tuple[TraceEvent, ...] = ()
    error: Optional[str] = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "final_output", _deep_freeze(self.final_output))
        object.__setattr__(self, "trace", _deep_freeze(tuple(self.trace)))
        object.__setattr__(self, "usage", _deep_freeze(self.usage))
        object.__setattr__(self, "artifacts", _deep_freeze(self.artifacts))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class Artifact:
    path: str
    content_hash: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    content: Optional[Any] = None
    producer: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", _deep_freeze(self.content))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class RunObservation:
    output: Any = None
    trace: Tuple[TraceEvent, ...] = ()
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    pre_state: Mapping[str, Any] = field(default_factory=dict)
    post_state: Mapping[str, Any] = field(default_factory=dict)
    workspace_root: Optional[Path] = None
    error: Optional[str] = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", _deep_freeze(self.output))
        object.__setattr__(self, "trace", _deep_freeze(tuple(self.trace)))
        object.__setattr__(self, "artifacts", _deep_freeze(self.artifacts))
        object.__setattr__(self, "pre_state", _deep_freeze(self.pre_state))
        object.__setattr__(self, "post_state", _deep_freeze(self.post_state))
        object.__setattr__(self, "usage", _deep_freeze(self.usage))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))
        if self.workspace_root is not None:
            object.__setattr__(self, "workspace_root", Path(self.workspace_root))

    @property
    def final_output(self) -> Any:
        return self.output


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    ref: str
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class GradeResult:
    grader_id: str
    status: GradeStatus
    version: str = ""
    score: Optional[float] = None
    hard: bool = True
    metrics: Mapping[str, Any] = field(default_factory=dict)
    evidence: Tuple[Any, ...] = ()
    missing: Tuple[str, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, GradeStatus):
            object.__setattr__(self, "status", GradeStatus(self.status))
        object.__setattr__(self, "metrics", _deep_freeze(self.metrics))
        object.__setattr__(self, "evidence", _deep_freeze(tuple(self.evidence)))
        object.__setattr__(self, "missing", tuple(self.missing))

    @property
    def passed(self) -> bool:
        return self.status == GradeStatus.PASS


Grade = GradeResult


@dataclass(frozen=True)
class CandidatePatch:
    base_hash: str
    unified_diff: str
    allowed_paths: Tuple[str, ...] = ()
    rationale: str = ""
    patch_hash: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_paths", tuple(self.allowed_paths))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class CandidateSnapshot:
    lineage_id: str
    snapshot_hash: str
    parent_hash: str
    round_index: int
    patch_hash: str
    dev_result_ref: Optional[str] = None
    status: CandidateStatus = CandidateStatus.PROPOSED
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, CandidateStatus):
            object.__setattr__(self, "status", CandidateStatus(self.status))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    base_hash: str
    lineage_id: str
    round_index: int
    patch: CandidatePatch
    snapshot: Optional[CandidateSnapshot] = None
    status: CandidateStatus = CandidateStatus.PROPOSED
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, CandidateStatus):
            object.__setattr__(self, "status", CandidateStatus(self.status))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


@dataclass(frozen=True)
class PatchConstraints:
    allowed_paths: Tuple[str, ...] = ()
    max_added_lines: Optional[int] = None
    max_candidates: Optional[int] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_paths", tuple(self.allowed_paths))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))


def as_primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {
            item.name: as_primitive(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): as_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [as_primitive(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(as_primitive(item) for item in value)
    return value


@runtime_checkable
class SubjectAdapter(Protocol):
    id: str

    def snapshot(
        self,
        subject_ref: str,
        params: Optional[Mapping[str, Any]] = None,
    ) -> SubjectSnapshot:
        ...

    def materialize(
        self,
        snapshot: SubjectSnapshot,
        destination: Path,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Path:
        ...


@runtime_checkable
class RuntimeAdapter(Protocol):
    id: str

    @property
    def capabilities(self) -> RuntimeCapabilities:
        ...

    async def execute(
        self,
        prepared: PreparedScenario,
        subject: SubjectSnapshot,
        context: RunContext,
    ) -> RuntimeResult:
        ...


@runtime_checkable
class ScenarioDriver(Protocol):
    id: str

    def required_capabilities(self, scenario: DriverScenarioView) -> FrozenSet[str]:
        ...

    async def prepare(
        self, scenario: DriverScenarioView, context: RunContext
    ) -> PreparedScenario:
        ...

    async def collect(
        self, prepared: PreparedScenario, result: RuntimeResult
    ) -> RunObservation:
        ...

    async def cleanup(self, prepared: PreparedScenario) -> None:
        ...


@runtime_checkable
class Grader(Protocol):
    id: str
    version: str

    async def evaluate(
        self,
        observation: RunObservation,
        oracle: FrozenOracle,
        params: Mapping[str, Any],
    ) -> GradeResult:
        ...


@runtime_checkable
class Optimizer(Protocol):
    id: str
    proposal_contract: str

    async def propose(
        self,
        base: SubjectSnapshot,
        diagnoses: Sequence[Any],
        constraints: PatchConstraints,
    ) -> Sequence[CandidatePatch]:
        ...


@runtime_checkable
class OptimizerPolicy(Protocol):
    def constrain(
        self, pack: FrozenEvalPack, global_budget: RunBudget
    ) -> PatchConstraints:
        ...


@runtime_checkable
class ComponentRegistryProtocol(Protocol):
    def subject_adapter(self, component_id: str) -> SubjectAdapter:
        ...

    def driver(self, component_id: str) -> ScenarioDriver:
        ...

    def grader(self, component_id: str) -> Grader:
        ...

    def optimizer(self, component_id: str) -> Optimizer:
        ...


@runtime_checkable
class EvalPackLoaderProtocol(Protocol):
    def load(self, ref: PackRef) -> FrozenEvalPack:
        ...

    def validate(
        self, pack: FrozenEvalPack, registry: ComponentRegistryProtocol
    ) -> PackReport:
        ...


@runtime_checkable
class Extension(Protocol):
    def register(self, registry: ComponentRegistryProtocol) -> None:
        ...
