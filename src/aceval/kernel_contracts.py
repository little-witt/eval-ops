"""Versioned contracts for the user-facing evaluation/iteration Kernel.

The desktop application should persist these records, but never persist raw
credentials. Secret values are referenced by environment variable name so the
same Kernel can later use an OS keychain-backed secret injector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from .environment_contracts import canonical_hash


KERNEL_CONFIG_API_VERSION = "aceval.kernel-config/v1"
KERNEL_INPUT_API_VERSION = "aceval.kernel-input/v1"
EVALUATION_DESIGN_API_VERSION = "aceval.kernel-evaluation-design/v1"
REMOTE_BATCH_API_VERSION = "aceval.kernel-remote-batch/v1"
ANALYSIS_DECISION_API_VERSION = "aceval.kernel-analysis-decision/v1"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
SKILL_HARNESS_OPERATIONS = ("auto", "repair", "tune", "extend", "discover", "create")


class KernelContractError(ValueError):
    pass


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise KernelContractError("%s must be a JSON object" % label)
    return value


def _text(value: Any, label: str, maximum: int = 8192) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum or "\x00" in value:
        raise KernelContractError("%s must be a trimmed non-empty string" % label)
    return value


def _optional_text(value: Any, label: str, maximum: int = 8192) -> Optional[str]:
    return None if value is None else _text(value, label, maximum)


def _positive_int(value: Any, label: str, maximum: int = 10_000_000) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > maximum:
        raise KernelContractError("%s must be a positive integer <= %d" % (label, maximum))
    return value


def _strict_clone(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise KernelContractError("%s must be strict JSON" % label) from exc


def _load(path: Union[str, Path], label: str) -> Mapping[str, Any]:
    source = Path(path).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise KernelContractError("%s must be a regular file" % label)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KernelContractError("cannot read %s" % label) from exc
    return _object(value, label)


def _reject_unknown(value: Mapping[str, Any], allowed: Sequence[str], label: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise KernelContractError("%s contains unsupported fields: %s" % (label, ", ".join(unknown)))


def _env(value: Any, label: str) -> str:
    text = _text(value, label, 128)
    if not _ENV_NAME.fullmatch(text):
        raise KernelContractError("%s must be an environment variable name" % label)
    return text


@dataclass(frozen=True)
class RepositoryConfig:
    ssh_url: str
    branch: str
    local_path: Optional[str] = None
    authorization_token_env: Optional[str] = None
    mount_path: Optional[str] = None

    def __post_init__(self) -> None:
        url = _text(self.ssh_url, "repository.ssh_url", 4096)
        if not (url.startswith("ssh://") or url.startswith("git@")):
            raise KernelContractError("repository.ssh_url must be an SSH Git URL")
        branch = _text(self.branch, "repository.branch", 512)
        if (
            not _BRANCH.fullmatch(branch)
            or ".." in branch
            or "//" in branch
            or "@{" in branch
            or branch.endswith((".", "/", ".lock"))
        ):
            raise KernelContractError("repository.branch must be a safe Git branch name")
        if self.local_path is not None:
            object.__setattr__(self, "local_path", str(Path(self.local_path).expanduser().resolve()))
        if self.authorization_token_env is not None:
            object.__setattr__(self, "authorization_token_env", _env(self.authorization_token_env, "repository.authorization_token_env"))
        if self.mount_path is not None:
            mount = _text(self.mount_path, "repository.mount_path")
            if not mount.startswith("/") or ".." in Path(mount).parts:
                raise KernelContractError("repository.mount_path must be an absolute safe path")

    @classmethod
    def from_mapping(cls, value: Any, label: str) -> "RepositoryConfig":
        item = _object(value, label)
        _reject_unknown(item, ("ssh_url", "branch", "local_path", "authorization_token_env", "mount_path"), label)
        return cls(
            ssh_url=item.get("ssh_url", ""),
            branch=item.get("branch", ""),
            local_path=item.get("local_path"),
            authorization_token_env=item.get("authorization_token_env"),
            mount_path=item.get("mount_path"),
        )

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "ssh_url": self.ssh_url,
            "branch": self.branch,
            "local_path": self.local_path,
            "authorization_token_env": self.authorization_token_env,
            "mount_path": self.mount_path,
        }


@dataclass(frozen=True)
class LocalAnalysisConfig:
    model_command: Tuple[str, ...]
    model_id: str
    api_base_url_env: Optional[str] = None
    api_key_env: Optional[str] = None
    env_allowlist: Tuple[str, ...] = ()
    timeout_seconds: int = 120
    max_prompt_chars: int = 48_000
    max_output_chars: int = 24_000

    def __post_init__(self) -> None:
        command = tuple(_text(item, "local_analysis.model_command[]", 4096) for item in self.model_command)
        if not command:
            raise KernelContractError("local_analysis.model_command must not be empty")
        object.__setattr__(self, "model_command", command)
        _text(self.model_id, "local_analysis.model_id", 256)
        if self.api_base_url_env is not None:
            object.__setattr__(self, "api_base_url_env", _env(self.api_base_url_env, "local_analysis.api_base_url_env"))
        if self.api_key_env is not None:
            object.__setattr__(self, "api_key_env", _env(self.api_key_env, "local_analysis.api_key_env"))
        object.__setattr__(self, "env_allowlist", tuple(_env(item, "local_analysis.env_allowlist[]") for item in self.env_allowlist))
        _positive_int(self.timeout_seconds, "local_analysis.timeout_seconds", 3600)
        _positive_int(self.max_prompt_chars, "local_analysis.max_prompt_chars", 4_000_000)
        _positive_int(self.max_output_chars, "local_analysis.max_output_chars", 1_000_000)

    @classmethod
    def from_mapping(cls, value: Any) -> "LocalAnalysisConfig":
        item = _object(value, "local_analysis")
        _reject_unknown(item, ("model_command", "model_id", "api_base_url_env", "api_key_env", "env_allowlist", "timeout_seconds", "max_prompt_chars", "max_output_chars"), "local_analysis")
        command = item.get("model_command", ())
        allowlist = item.get("env_allowlist", ())
        if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
            raise KernelContractError("local_analysis.model_command must be an array")
        if isinstance(allowlist, (str, bytes)) or not isinstance(allowlist, Sequence):
            raise KernelContractError("local_analysis.env_allowlist must be an array")
        return cls(
            model_command=tuple(command),
            model_id=item.get("model_id", ""),
            api_base_url_env=item.get("api_base_url_env"),
            api_key_env=item.get("api_key_env"),
            env_allowlist=tuple(allowlist),
            timeout_seconds=item.get("timeout_seconds", 120),
            max_prompt_chars=item.get("max_prompt_chars", 48_000),
            max_output_chars=item.get("max_output_chars", 24_000),
        )

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "model_command": list(self.model_command),
            "model_id": self.model_id,
            "api_base_url_env": self.api_base_url_env,
            "api_key_env": self.api_key_env,
            "env_allowlist": list(self.env_allowlist),
            "timeout_seconds": self.timeout_seconds,
            "max_prompt_chars": self.max_prompt_chars,
            "max_output_chars": self.max_output_chars,
        }


@dataclass(frozen=True)
class RemoteAgentConfig:
    profile_path: str
    max_parallel: int = 8
    poll_interval_seconds: int = 5
    max_wait_seconds: int = 1200

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_path", str(Path(_text(self.profile_path, "remote_agent.profile_path")).expanduser().resolve()))
        _positive_int(self.max_parallel, "remote_agent.max_parallel", 64)
        _positive_int(self.poll_interval_seconds, "remote_agent.poll_interval_seconds", 300)
        _positive_int(self.max_wait_seconds, "remote_agent.max_wait_seconds", 86_400)

    @classmethod
    def from_mapping(cls, value: Any) -> "RemoteAgentConfig":
        item = _object(value, "remote_agent")
        _reject_unknown(item, ("profile_path", "max_parallel", "poll_interval_seconds", "max_wait_seconds"), "remote_agent")
        return cls(
            profile_path=item.get("profile_path", ""),
            max_parallel=item.get("max_parallel", 8),
            poll_interval_seconds=item.get("poll_interval_seconds", 5),
            max_wait_seconds=item.get("max_wait_seconds", 1200),
        )

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "profile_path": self.profile_path,
            "max_parallel": self.max_parallel,
            "poll_interval_seconds": self.poll_interval_seconds,
            "max_wait_seconds": self.max_wait_seconds,
        }


@dataclass(frozen=True)
class KernelPolicy:
    max_generated_cases: int = 12
    max_rounds: int = 5
    pass_verification_runs: int = 1
    max_analysis_evidence_chars_per_case: int = 4000
    max_remote_prompt_chars: int = 12_000
    max_total_remote_sessions: int = 200
    min_improvement: float = 0.01
    convergence_patience: int = 2
    optimization_editable_patterns: Tuple[str, ...] = (
        "SKILL.md", "src/SKILL.md", "scripts/**", "references/**",
        "workflow/**", "knowledge/**", "specs/**", "config/**", "assets/**",
    )
    optimization_validation_commands: Tuple[Tuple[str, ...], ...] = ()
    max_optimization_changed_files: int = 8
    max_optimization_added_lines: int = 300
    max_optimization_changed_bytes: int = 512 * 1024
    max_optimization_context_chars: int = 120_000

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_generated_cases", 100), ("max_rounds", 50),
            ("pass_verification_runs", 1), ("max_analysis_evidence_chars_per_case", 100_000),
            ("max_remote_prompt_chars", 100_000),
            ("max_total_remote_sessions", 10_000), ("convergence_patience", 20),
            ("max_optimization_changed_files", 100),
            ("max_optimization_added_lines", 100_000),
            ("max_optimization_changed_bytes", 32 * 1024 * 1024),
            ("max_optimization_context_chars", 4_000_000),
        ):
            _positive_int(getattr(self, name), "policy.%s" % name, maximum)
        if isinstance(self.min_improvement, bool) or not isinstance(self.min_improvement, (int, float)) or not 0 <= float(self.min_improvement) <= 1:
            raise KernelContractError("policy.min_improvement must be between 0 and 1")
        patterns = tuple(_text(item, "policy.optimization_editable_patterns[]", 512) for item in self.optimization_editable_patterns)
        if not patterns:
            raise KernelContractError("policy.optimization_editable_patterns must not be empty")
        object.__setattr__(self, "optimization_editable_patterns", patterns)
        commands = []
        for index, command in enumerate(self.optimization_validation_commands):
            if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
                raise KernelContractError("policy.optimization_validation_commands[%d] must be an argv array" % index)
            argv = tuple(_text(item, "policy.optimization_validation_commands[][]", 4096) for item in command)
            if not argv:
                raise KernelContractError("policy.optimization_validation_commands[] must not be empty")
            commands.append(argv)
        if len(commands) > 20:
            raise KernelContractError("policy.optimization_validation_commands supports at most 20 commands")
        object.__setattr__(self, "optimization_validation_commands", tuple(commands))

    @classmethod
    def from_mapping(cls, value: Any) -> "KernelPolicy":
        item = _object(value, "policy")
        allowed = (
            "max_generated_cases", "max_rounds", "pass_verification_runs",
            "max_analysis_evidence_chars_per_case", "max_remote_prompt_chars",
            "max_total_remote_sessions", "min_improvement", "convergence_patience",
            "optimization_editable_patterns", "optimization_validation_commands",
            "max_optimization_changed_files", "max_optimization_added_lines",
            "max_optimization_changed_bytes", "max_optimization_context_chars",
        )
        _reject_unknown(item, allowed, "policy")
        for key in ("optimization_editable_patterns", "optimization_validation_commands"):
            value = item.get(key, ())
            if key in item and (isinstance(value, (str, bytes)) or not isinstance(value, Sequence)):
                raise KernelContractError("policy.%s must be an array" % key)
        return cls(**{key: item[key] for key in allowed if key in item})

    def to_dict(self) -> Mapping[str, Any]:
        value = dict(self.__dict__)
        value["optimization_editable_patterns"] = list(self.optimization_editable_patterns)
        value["optimization_validation_commands"] = [list(command) for command in self.optimization_validation_commands]
        return value


@dataclass(frozen=True)
class KernelConfig:
    api_version: str
    skill_repository: RepositoryConfig
    code_repository: Optional[RepositoryConfig]
    local_analysis: LocalAnalysisConfig
    remote_agent: RemoteAgentConfig
    policy: KernelPolicy = field(default_factory=KernelPolicy)

    def __post_init__(self) -> None:
        if self.api_version != KERNEL_CONFIG_API_VERSION:
            raise KernelContractError("unsupported Kernel config api_version")

    @classmethod
    def from_mapping(cls, value: Any) -> "KernelConfig":
        item = _object(value, "Kernel config")
        _reject_unknown(item, ("api_version", "skill_repository", "code_repository", "local_analysis", "remote_agent", "policy"), "Kernel config")
        return cls(
            api_version=item.get("api_version", ""),
            skill_repository=RepositoryConfig.from_mapping(item.get("skill_repository"), "skill_repository"),
            code_repository=RepositoryConfig.from_mapping(item["code_repository"], "code_repository") if item.get("code_repository") is not None else None,
            local_analysis=LocalAnalysisConfig.from_mapping(item.get("local_analysis")),
            remote_agent=RemoteAgentConfig.from_mapping(item.get("remote_agent")),
            policy=KernelPolicy.from_mapping(item.get("policy", {})),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "KernelConfig":
        return cls.from_mapping(_load(path, "Kernel config"))

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "skill_repository": self.skill_repository.to_dict(),
            "code_repository": self.code_repository.to_dict() if self.code_repository else None,
            "local_analysis": self.local_analysis.to_dict(),
            "remote_agent": self.remote_agent.to_dict(),
            "policy": self.policy.to_dict(),
        }


@dataclass(frozen=True)
class UserCase:
    id: str
    prompt: str
    expected_output: Any = None
    has_expected_output: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        case_id = _text(self.id, "case.id", 256)
        if not _CASE_ID.fullmatch(case_id):
            raise KernelContractError("case.id must be a safe stable identifier")
        _text(self.prompt, "case.prompt", 32_000)
        object.__setattr__(self, "metadata", _strict_clone(_object(self.metadata, "case.metadata"), "case.metadata"))
        if self.has_expected_output:
            object.__setattr__(self, "expected_output", _strict_clone(self.expected_output, "case.expected_output"))

    @classmethod
    def from_mapping(cls, value: Any) -> "UserCase":
        item = _object(value, "case")
        _reject_unknown(item, ("id", "prompt", "expected_output", "metadata"), "case")
        return cls(
            id=item.get("id", ""),
            prompt=item.get("prompt", ""),
            expected_output=item.get("expected_output"),
            has_expected_output="expected_output" in item,
            metadata=item.get("metadata", {}),
        )

    def to_dict(self) -> Mapping[str, Any]:
        result = {"id": self.id, "prompt": self.prompt, "metadata": dict(self.metadata)}
        if self.has_expected_output:
            result["expected_output"] = self.expected_output
        return result


@dataclass(frozen=True)
class KernelInput:
    api_version: str
    skill_name: str
    goal: Optional[str] = None
    standards: Tuple[str, ...] = ()
    cases: Tuple[UserCase, ...] = ()
    notes: Optional[str] = None
    evalpack_path: Optional[str] = None
    operation: str = "auto"
    capabilities: Tuple[str, ...] = ()
    non_goals: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.api_version != KERNEL_INPUT_API_VERSION:
            raise KernelContractError("unsupported Kernel input api_version")
        _text(self.skill_name, "skill_name", 256)
        if self.goal is not None:
            _text(self.goal, "goal", 16_000)
        object.__setattr__(self, "standards", tuple(_text(item, "standards[]", 4096) for item in self.standards))
        object.__setattr__(self, "cases", tuple(self.cases))
        if len({case.id for case in self.cases}) != len(self.cases):
            raise KernelContractError("case ids must be unique")
        if self.notes is not None:
            _text(self.notes, "notes", 16_000)
        if self.evalpack_path is not None:
            object.__setattr__(self, "evalpack_path", str(Path(_text(self.evalpack_path, "evalpack_path")).expanduser().resolve()))
        operation = _text(self.operation, "operation", 32).lower()
        if operation not in SKILL_HARNESS_OPERATIONS:
            raise KernelContractError("operation must be auto, repair, tune, extend, discover, or create")
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "capabilities", tuple(_text(item, "capabilities[]", 4096) for item in self.capabilities))
        object.__setattr__(self, "non_goals", tuple(_text(item, "non_goals[]", 4096) for item in self.non_goals))
        if operation == "create" and not (self.goal or self.capabilities or self.cases or self.evalpack_path):
            raise KernelContractError("create operation requires a goal, capability, Case, or custom EvalPack")

    @property
    def mode(self) -> str:
        if self.evalpack_path:
            return "custom_evalpack"
        if self.cases:
            return "cases_with_expected" if all(case.has_expected_output for case in self.cases) else "cases_or_goal"
        if self.goal or self.standards:
            return "goal_only"
        return "exploratory"

    @property
    def effective_goal(self) -> str:
        return self.goal or (
            "探索并提升 %s，使其更稳定、更准确、更高效地完成其声明目标" % self.skill_name
        )

    @property
    def effective_standards(self) -> Tuple[str, ...]:
        return self.standards or (
            "完成 Skill 声明的主要目标",
            "遵循 Skill 的关键执行步骤且不产生越权副作用",
            "输出具备可验证证据，避免无依据结论",
        )

    def intent_mode(self, *, has_existing_skill: bool) -> str:
        """Resolve the Harness operation without conflating it with input richness."""

        if self.operation != "auto":
            if self.operation == "create" and has_existing_skill:
                raise KernelContractError("create operation requires a repository without an existing SKILL.md")
            if self.operation in ("repair", "tune", "extend", "discover") and not has_existing_skill:
                raise KernelContractError("%s operation requires an existing SKILL.md" % self.operation)
            return self.operation
        if not has_existing_skill:
            if not (self.goal or self.capabilities or self.cases or self.evalpack_path):
                raise KernelContractError("creating a Skill requires a goal, capability, Case, or custom EvalPack")
            return "create"
        if self.capabilities:
            return "extend"
        if not (self.goal or self.standards or self.cases or self.evalpack_path):
            return "discover"
        return "auto"

    @classmethod
    def from_mapping(cls, value: Any) -> "KernelInput":
        item = _object(value, "Kernel input")
        _reject_unknown(item, ("api_version", "skill_name", "goal", "standards", "cases", "notes", "evalpack_path", "operation", "capabilities", "non_goals"), "Kernel input")
        standards = item.get("standards", ())
        cases = item.get("cases", ())
        capabilities = item.get("capabilities", ())
        non_goals = item.get("non_goals", ())
        if isinstance(standards, (str, bytes)) or not isinstance(standards, Sequence):
            raise KernelContractError("standards must be an array")
        if isinstance(cases, (str, bytes)) or not isinstance(cases, Sequence):
            raise KernelContractError("cases must be an array")
        if isinstance(capabilities, (str, bytes)) or not isinstance(capabilities, Sequence):
            raise KernelContractError("capabilities must be an array")
        if isinstance(non_goals, (str, bytes)) or not isinstance(non_goals, Sequence):
            raise KernelContractError("non_goals must be an array")
        return cls(
            api_version=item.get("api_version", ""),
            skill_name=item.get("skill_name", ""),
            goal=item.get("goal"),
            standards=tuple(standards),
            cases=tuple(UserCase.from_mapping(case) for case in cases),
            notes=item.get("notes"),
            evalpack_path=item.get("evalpack_path"),
            operation=item.get("operation", "auto"),
            capabilities=tuple(capabilities),
            non_goals=tuple(non_goals),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "KernelInput":
        return cls.from_mapping(_load(path, "Kernel input"))

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "skill_name": self.skill_name,
            "goal": self.goal,
            "standards": list(self.standards),
            "cases": [case.to_dict() for case in self.cases],
            "notes": self.notes,
            "evalpack_path": self.evalpack_path,
            "operation": self.operation,
            "capabilities": list(self.capabilities),
            "non_goals": list(self.non_goals),
        }


def contract_hash(value: Any) -> str:
    return canonical_hash(value.to_dict() if hasattr(value, "to_dict") else value)


__all__ = [
    "ANALYSIS_DECISION_API_VERSION", "EVALUATION_DESIGN_API_VERSION", "KERNEL_CONFIG_API_VERSION",
    "KERNEL_INPUT_API_VERSION", "REMOTE_BATCH_API_VERSION", "KernelConfig", "KernelContractError",
    "KernelInput", "KernelPolicy", "LocalAnalysisConfig", "RemoteAgentConfig", "RepositoryConfig", "SKILL_HARNESS_OPERATIONS",
    "UserCase", "contract_hash",
]
