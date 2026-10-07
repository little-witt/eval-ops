"""Deterministic, source-grounded analysis for UTF-8 ``SKILL.md`` files.

The analyzer is intentionally conservative.  It inventories declarations that
can be pointed back to a stable line range; it does not ask a model to invent a
workflow or silently promote an inference into a contract.  Richer semantic
analysis can be layered on top of this module as long as it preserves the
source bindings defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .contracts import SubjectSnapshot
from .subjects import SkillMarkdownSubjectAdapter, hash_skill_subject


SKILL_ANALYSIS_API_VERSION = "aceval.skill-analysis/v1"
SOURCE_BINDINGS = frozenset(("explicit", "user_confirmed", "inferred", "unknown"))
_CONTRACT_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")


class SkillAnalysisError(ValueError):
    """The Skill cannot be analyzed without weakening source integrity."""


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillAnalysisError("%s must be a non-empty string" % label)
    return value.strip()


def _required_id(value: Any, label: str) -> str:
    result = _required_string(value, label)
    if not _CONTRACT_ID.match(result):
        raise SkillAnalysisError(
            "%s must match %s" % (label, _CONTRACT_ID.pattern)
        )
    return result


def _string_tuple(value: Iterable[Any], label: str) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise SkillAnalysisError("%s must be a sequence of strings" % label)
    result = []
    for item in value:
        result.append(_required_string(item, "%s item" % label))
    return tuple(result)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SkillAnalysisError("%s must be a JSON object" % label)
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SkillAnalysisError("%s must be a JSON array" % label)
    return value


def _strict_keys(
    value: Mapping[str, Any], required: Iterable[str], optional: Iterable[str], label: str
) -> None:
    required_set = frozenset(required)
    allowed = required_set | frozenset(optional)
    missing = required_set.difference(value)
    unknown = set(value).difference(allowed)
    if missing:
        raise SkillAnalysisError(
            "%s is missing fields: %s" % (label, ", ".join(sorted(missing)))
        )
    if unknown:
        raise SkillAnalysisError(
            "%s has unsupported fields: %s" % (label, ", ".join(sorted(unknown)))
        )


def _sha256(value: bytes) -> str:
    return "sha256:%s" % hashlib.sha256(value).hexdigest()


def _normalized_subject_hash(value: str) -> str:
    value = _required_string(value, "subject_hash")
    if value.startswith("sha256:"):
        digest = value[7:]
    else:
        digest = value
    if not re.match(r"^[0-9a-f]{64}$", digest):
        raise SkillAnalysisError("subject_hash must be a SHA-256 digest")
    return "sha256:%s" % digest


@dataclass(frozen=True)
class SourceRef:
    path: str
    start_line: int
    end_line: int
    quote_sha256: str
    binding: str = "explicit"

    def __post_init__(self) -> None:
        path = _required_string(self.path, "source_ref.path")
        pure = PurePath(path)
        if pure.is_absolute() or ".." in pure.parts:
            raise SkillAnalysisError("source_ref.path must be a safe relative path")
        if (
            not isinstance(self.start_line, int)
            or isinstance(self.start_line, bool)
            or self.start_line <= 0
            or not isinstance(self.end_line, int)
            or isinstance(self.end_line, bool)
            or self.end_line < self.start_line
        ):
            raise SkillAnalysisError("source_ref line range is invalid")
        quote_hash = _normalized_subject_hash(self.quote_sha256)
        binding = _required_string(self.binding, "source_ref.binding")
        if binding not in SOURCE_BINDINGS:
            raise SkillAnalysisError("unsupported source_ref binding: %s" % binding)
        object.__setattr__(self, "path", pure.as_posix())
        object.__setattr__(self, "quote_sha256", quote_hash)
        object.__setattr__(self, "binding", binding)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "quote_sha256": self.quote_sha256,
            "binding": self.binding,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SourceRef":
        data = _mapping(value, "source_ref")
        _strict_keys(
            data,
            ("path", "start_line", "end_line", "quote_sha256", "binding"),
            (),
            "source_ref",
        )
        return cls(**dict(data))


@dataclass(frozen=True)
class SkillSection:
    id: str
    title: str
    level: int
    source_ref: SourceRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_id(self.id, "section.id"))
        object.__setattr__(self, "title", _required_string(self.title, "section.title"))
        if (
            not isinstance(self.level, int)
            or isinstance(self.level, bool)
            or not 0 <= self.level <= 6
        ):
            raise SkillAnalysisError("section.level must be between 0 and 6")
        if not isinstance(self.source_ref, SourceRef):
            raise SkillAnalysisError("section.source_ref must be a SourceRef")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "level": self.level,
            "source_ref": self.source_ref.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SkillSection":
        data = _mapping(value, "section")
        _strict_keys(data, ("id", "title", "level", "source_ref"), (), "section")
        return cls(
            id=data["id"],
            title=data["title"],
            level=data["level"],
            source_ref=SourceRef.from_dict(data["source_ref"]),
        )


@dataclass(frozen=True)
class CapabilityBranch:
    id: str
    condition: str
    source_ref: SourceRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_id(self.id, "branch.id"))
        object.__setattr__(
            self, "condition", _required_string(self.condition, "branch.condition")
        )
        if not isinstance(self.source_ref, SourceRef):
            raise SkillAnalysisError("branch.source_ref must be a SourceRef")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "condition": self.condition,
            "source_ref": self.source_ref.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CapabilityBranch":
        data = _mapping(value, "branch")
        _strict_keys(data, ("id", "condition", "source_ref"), (), "branch")
        return cls(
            id=data["id"],
            condition=data["condition"],
            source_ref=SourceRef.from_dict(data["source_ref"]),
        )


@dataclass(frozen=True)
class ToolReference:
    name: str
    kind: str
    required_runtime_capabilities: Tuple[str, ...]
    source_refs: Tuple[SourceRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_string(self.name, "tool.name"))
        object.__setattr__(self, "kind", _required_string(self.kind, "tool.kind"))
        object.__setattr__(
            self,
            "required_runtime_capabilities",
            _string_tuple(
                self.required_runtime_capabilities,
                "tool.required_runtime_capabilities",
            ),
        )
        refs = tuple(self.source_refs)
        if not refs or not all(isinstance(item, SourceRef) for item in refs):
            raise SkillAnalysisError("tool.source_refs must contain SourceRef values")
        object.__setattr__(self, "source_refs", refs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "required_runtime_capabilities": list(
                self.required_runtime_capabilities
            ),
            "source_refs": [item.to_dict() for item in self.source_refs],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ToolReference":
        data = _mapping(value, "tool")
        _strict_keys(
            data,
            ("name", "kind", "required_runtime_capabilities", "source_refs"),
            (),
            "tool",
        )
        return cls(
            name=data["name"],
            kind=data["kind"],
            required_runtime_capabilities=_sequence(
                data["required_runtime_capabilities"],
                "tool.required_runtime_capabilities",
            ),
            source_refs=tuple(
                SourceRef.from_dict(item)
                for item in _sequence(data["source_refs"], "tool.source_refs")
            ),
        )


@dataclass(frozen=True)
class Capability:
    id: str
    name: str
    source_refs: Tuple[SourceRef, ...]
    inputs: Tuple[str, ...] = ()
    outputs: Tuple[str, ...] = ()
    preconditions: Tuple[str, ...] = ()
    steps: Tuple[str, ...] = ()
    branches: Tuple[CapabilityBranch, ...] = ()
    tools: Tuple[str, ...] = ()
    state_transitions: Tuple[str, ...] = ()
    side_effects: Tuple[str, ...] = ()
    risks: Tuple[str, ...] = ()
    inferred: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_id(self.id, "capability.id"))
        object.__setattr__(self, "name", _required_string(self.name, "capability.name"))
        refs = tuple(self.source_refs)
        if not refs or not all(isinstance(item, SourceRef) for item in refs):
            raise SkillAnalysisError(
                "capability.source_refs must contain SourceRef values"
            )
        object.__setattr__(self, "source_refs", refs)
        for name in (
            "inputs",
            "outputs",
            "preconditions",
            "steps",
            "tools",
            "state_transitions",
            "side_effects",
            "risks",
        ):
            object.__setattr__(self, name, _string_tuple(getattr(self, name), name))
        branches = tuple(self.branches)
        if not all(isinstance(item, CapabilityBranch) for item in branches):
            raise SkillAnalysisError("capability.branches must contain branches")
        object.__setattr__(self, "branches", branches)
        if not isinstance(self.inferred, bool):
            raise SkillAnalysisError("capability.inferred must be a boolean")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source_refs": [item.to_dict() for item in self.source_refs],
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "preconditions": list(self.preconditions),
            "steps": list(self.steps),
            "branches": [item.to_dict() for item in self.branches],
            "tools": list(self.tools),
            "state_transitions": list(self.state_transitions),
            "side_effects": list(self.side_effects),
            "risks": list(self.risks),
            "inferred": self.inferred,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "Capability":
        data = _mapping(value, "capability")
        fields = (
            "id",
            "name",
            "source_refs",
            "inputs",
            "outputs",
            "preconditions",
            "steps",
            "branches",
            "tools",
            "state_transitions",
            "side_effects",
            "risks",
            "inferred",
        )
        _strict_keys(data, fields, (), "capability")
        return cls(
            id=data["id"],
            name=data["name"],
            source_refs=tuple(
                SourceRef.from_dict(item)
                for item in _sequence(data["source_refs"], "capability.source_refs")
            ),
            inputs=_sequence(data["inputs"], "capability.inputs"),
            outputs=_sequence(data["outputs"], "capability.outputs"),
            preconditions=_sequence(
                data["preconditions"], "capability.preconditions"
            ),
            steps=_sequence(data["steps"], "capability.steps"),
            branches=tuple(
                CapabilityBranch.from_dict(item)
                for item in _sequence(data["branches"], "capability.branches")
            ),
            tools=_sequence(data["tools"], "capability.tools"),
            state_transitions=_sequence(
                data["state_transitions"], "capability.state_transitions"
            ),
            side_effects=_sequence(
                data["side_effects"], "capability.side_effects"
            ),
            risks=_sequence(data["risks"], "capability.risks"),
            inferred=data["inferred"],
        )


@dataclass(frozen=True)
class SkillAmbiguity:
    id: str
    reason_code: str
    message: str
    source_ref: SourceRef
    capability_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_id(self.id, "ambiguity.id"))
        object.__setattr__(
            self,
            "reason_code",
            _required_string(self.reason_code, "ambiguity.reason_code"),
        )
        object.__setattr__(
            self, "message", _required_string(self.message, "ambiguity.message")
        )
        if not isinstance(self.source_ref, SourceRef):
            raise SkillAnalysisError("ambiguity.source_ref must be a SourceRef")
        if self.capability_id is not None:
            object.__setattr__(
                self,
                "capability_id",
                _required_string(self.capability_id, "ambiguity.capability_id"),
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "reason_code": self.reason_code,
            "message": self.message,
            "source_ref": self.source_ref.to_dict(),
            "capability_id": self.capability_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SkillAmbiguity":
        data = _mapping(value, "ambiguity")
        _strict_keys(
            data,
            ("id", "reason_code", "message", "source_ref", "capability_id"),
            (),
            "ambiguity",
        )
        return cls(
            id=data["id"],
            reason_code=data["reason_code"],
            message=data["message"],
            source_ref=SourceRef.from_dict(data["source_ref"]),
            capability_id=data["capability_id"],
        )


@dataclass(frozen=True)
class CapabilityGraph:
    subject_hash: str
    source_path: str
    sections: Tuple[SkillSection, ...]
    capabilities: Tuple[Capability, ...]
    tools: Tuple[ToolReference, ...]
    ambiguities: Tuple[SkillAmbiguity, ...]
    api_version: str = SKILL_ANALYSIS_API_VERSION

    def __post_init__(self) -> None:
        if self.api_version != SKILL_ANALYSIS_API_VERSION:
            raise SkillAnalysisError(
                "unsupported skill analysis api_version: %s" % self.api_version
            )
        object.__setattr__(
            self, "subject_hash", _normalized_subject_hash(self.subject_hash)
        )
        source_path = _required_string(self.source_path, "source_path")
        pure = PurePath(source_path)
        if pure.is_absolute() or ".." in pure.parts:
            raise SkillAnalysisError("source_path must be a safe relative path")
        object.__setattr__(self, "source_path", pure.as_posix())
        for name, expected in (
            ("sections", SkillSection),
            ("capabilities", Capability),
            ("tools", ToolReference),
            ("ambiguities", SkillAmbiguity),
        ):
            values = tuple(getattr(self, name))
            if not all(isinstance(item, expected) for item in values):
                raise SkillAnalysisError("%s contains an invalid value" % name)
            object.__setattr__(self, name, values)
        if not self.capabilities:
            raise SkillAnalysisError("capability graph must contain a capability")
        _ensure_unique((item.id for item in self.sections), "section id")
        _ensure_unique((item.id for item in self.capabilities), "capability id")
        _ensure_unique((item.name for item in self.tools), "tool name")
        _ensure_unique((item.id for item in self.ambiguities), "ambiguity id")
        _ensure_unique(
            (
                branch.id
                for capability in self.capabilities
                for branch in capability.branches
            ),
            "branch id",
        )
        capability_ids = {item.id for item in self.capabilities}
        tool_names = {item.name for item in self.tools}
        for capability in self.capabilities:
            unknown_tools = set(capability.tools).difference(tool_names)
            if unknown_tools:
                raise SkillAnalysisError(
                    "capability %s references unknown tools: %s"
                    % (capability.id, ", ".join(sorted(unknown_tools)))
                )
        for ambiguity in self.ambiguities:
            if (
                ambiguity.capability_id is not None
                and ambiguity.capability_id not in capability_ids
            ):
                raise SkillAnalysisError(
                    "ambiguity references unknown capability: %s"
                    % ambiguity.capability_id
                )
        refs = [item.source_ref for item in self.sections]
        for capability in self.capabilities:
            refs.extend(capability.source_refs)
            refs.extend(branch.source_ref for branch in capability.branches)
        for tool in self.tools:
            refs.extend(tool.source_refs)
        refs.extend(item.source_ref for item in self.ambiguities)
        if any(ref.path != self.source_path for ref in refs):
            raise SkillAnalysisError(
                "all source references must use the graph source_path"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "api_version": self.api_version,
            "subject_hash": self.subject_hash,
            "source_path": self.source_path,
            "sections": [item.to_dict() for item in self.sections],
            "capabilities": [item.to_dict() for item in self.capabilities],
            "tools": [item.to_dict() for item in self.tools],
            "ambiguities": [item.to_dict() for item in self.ambiguities],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "CapabilityGraph":
        data = _mapping(value, "capability graph")
        fields = (
            "api_version",
            "subject_hash",
            "source_path",
            "sections",
            "capabilities",
            "tools",
            "ambiguities",
        )
        _strict_keys(data, fields, (), "capability graph")
        return cls(
            api_version=data["api_version"],
            subject_hash=data["subject_hash"],
            source_path=data["source_path"],
            sections=tuple(
                SkillSection.from_dict(item)
                for item in _sequence(data["sections"], "capability graph sections")
            ),
            capabilities=tuple(
                Capability.from_dict(item)
                for item in _sequence(
                    data["capabilities"], "capability graph capabilities"
                )
            ),
            tools=tuple(
                ToolReference.from_dict(item)
                for item in _sequence(data["tools"], "capability graph tools")
            ),
            ambiguities=tuple(
                SkillAmbiguity.from_dict(item)
                for item in _sequence(
                    data["ambiguities"], "capability graph ambiguities"
                )
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> "CapabilityGraph":
        try:
            decoded = json.loads(
                value,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_object,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise SkillAnalysisError("invalid capability graph JSON") from exc
        return cls.from_dict(decoded)

    def validate_source(self, markdown: str) -> None:
        """Verify every source span against the supplied frozen Markdown."""

        if not isinstance(markdown, str):
            raise SkillAnalysisError("markdown must be a string")
        lines = markdown.splitlines(keepends=True)
        refs = []  # type: List[SourceRef]
        refs.extend(item.source_ref for item in self.sections)
        for capability in self.capabilities:
            refs.extend(capability.source_refs)
            refs.extend(branch.source_ref for branch in capability.branches)
        for tool in self.tools:
            refs.extend(tool.source_refs)
        refs.extend(item.source_ref for item in self.ambiguities)
        for ref in refs:
            if ref.path != self.source_path or ref.end_line > len(lines):
                raise SkillAnalysisError("source_ref is outside the frozen source")
            quote = "".join(lines[ref.start_line - 1 : ref.end_line]).encode("utf-8")
            if _sha256(quote) != ref.quote_sha256:
                raise SkillAnalysisError("source_ref quote hash mismatch")


def _reject_json_constant(value: str) -> Any:
    raise ValueError("non-standard JSON constant: %s" % value)


def _reject_duplicate_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}  # type: Dict[str, Any]
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _ensure_unique(values: Iterable[str], label: str) -> None:
    seen = set()
    duplicates = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise SkillAnalysisError(
            "duplicate %s: %s" % (label, ", ".join(sorted(duplicates)))
        )


@dataclass(frozen=True)
class _FrozenSource:
    text: str
    source_path: str
    subject_hash: str


@dataclass(frozen=True)
class _Heading:
    title: str
    level: int
    line: int
    end_line: int


_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+?)\s*$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")
_LABEL = re.compile(
    r"^\s*(?:[-*+]\s*)?(input|inputs|output|outputs|precondition|preconditions|"
    r"输入|输出|前置条件)\s*[:：]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_CONDITIONAL = re.compile(
    r"(?:^|[.;。；]\s*)(if\b|when\b|unless\b|otherwise\b|on\s+failure\b|"
    r"如果|若|当.+时|否则|失败时)",
    re.IGNORECASE,
)
_VAGUE = re.compile(
    r"\b(?:todo|tbd|fixme|as needed|as appropriate|if appropriate|when appropriate|reasonable|"
    r"best effort|high quality|unspecified|not specified)\b|待定|未定义|不明确|"
    r"必要时|适当(?:地)?|尽量|合理(?:地)?|高质量",
    re.IGNORECASE,
)
_EXPLICIT_PLACEHOLDER = re.compile(r"\b(?:todo|tbd|fixme)\b|待定|未定义", re.I)
_RISK = re.compile(
    r"\b(?:risk|warning|never|must not|do not|unsafe|security|danger)\b|"
    r"风险|警告|禁止|不得|不可|安全|危险",
    re.IGNORECASE,
)
_SIDE_EFFECT = re.compile(
    r"\b(?:write|create|save|delete|remove|update|modify|send|publish|commit)\b|"
    r"写入|创建|保存|删除|更新|修改|发送|发布|提交",
    re.IGNORECASE,
)
_STATE = re.compile(
    r"\b(?:state|status|transition|from\s+.+\s+to)\b|状态|阶段|变更为|切换为",
    re.IGNORECASE,
)
_ACTION = re.compile(
    r"\b(?:analy[sz]e|validate|check|generate|create|produce|review|convert|"
    r"translate|summari[sz]e|extract|detect|repair|optimi[sz]e|run|read|write|"
    r"return|use|call|inspect|classify)\b|"
    r"分析|校验|验证|检查|生成|创建|输出|审查|转换|翻译|总结|提取|检测|修复|"
    r"优化|执行|读取|写入|返回|使用|调用|分类",
    re.IGNORECASE,
)
_TOOL_CONTEXT = re.compile(
    r"\b(?:tool|tools|use|using|call|invoke|command|cli|run|execute)\b|"
    r"工具|使用|调用|命令|运行|执行",
    re.IGNORECASE,
)
_BACKTICK = re.compile(r"`([^`\n]+)`")
_SIMPLE_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_KNOWN_TOOLS = frozenset(
    (
        "list_files",
        "read_file",
        "write_file",
        "process_exec",
        "shell",
        "browser",
        "curl",
        "git",
        "python",
        "python3",
        "node",
        "npm",
    )
)
_GENERIC_HEADINGS = frozenset(
    (
        "overview",
        "description",
        "instructions",
        "workflow",
        "steps",
        "tools",
        "inputs",
        "outputs",
        "preconditions",
        "requirements",
        "limitations",
        "examples",
        "notes",
        "背景",
        "说明",
        "工作流",
        "步骤",
        "工具",
        "输入",
        "输出",
        "前置条件",
        "要求",
        "限制",
        "示例",
        "注意事项",
        "能力",
        "功能",
        "capabilities",
        "capability",
    )
)


def _slug(value: str, fallback: str) -> str:
    lowered = value.lower().strip()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if lowered:
        return lowered[:64]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return "%s-%s" % (fallback, digest)


def _unique_id(prefix: str, label: str, used: set) -> str:
    base = "%s.%s" % (prefix, _slug(label, "item"))
    candidate = base
    index = 2
    while candidate in used:
        candidate = "%s-%d" % (base, index)
        index += 1
    used.add(candidate)
    return candidate


def _source_ref(
    lines: Sequence[str], source_path: str, start_line: int, end_line: int, binding: str
) -> SourceRef:
    quote = "".join(lines[start_line - 1 : end_line]).encode("utf-8")
    return SourceRef(
        path=source_path,
        start_line=start_line,
        end_line=end_line,
        quote_sha256=_sha256(quote),
        binding=binding,
    )


def _load_frozen_source(
    source: Union[str, bytes, os.PathLike, SubjectSnapshot], source_path: str
) -> _FrozenSource:
    if isinstance(source, SubjectSnapshot):
        skill_bytes = source.files.get("SKILL.md")
        manifest_bytes = source.files.get("subject.json")
        resource_files = {
            str(path): content
            for path, content in source.files.items()
            if path not in ("SKILL.md", "subject.json")
        }
        if skill_bytes is None:
            if not isinstance(source.content, str):
                raise SkillAnalysisError(
                    "SubjectSnapshot must freeze SKILL.md bytes or string content"
                )
            skill_bytes = source.content.encode("utf-8")
        if not isinstance(skill_bytes, bytes):
            raise SkillAnalysisError("SubjectSnapshot SKILL.md must contain bytes")
        if manifest_bytes is not None and not isinstance(manifest_bytes, bytes):
            raise SkillAnalysisError("SubjectSnapshot subject.json must contain bytes")
        try:
            text = skill_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillAnalysisError("SKILL.md must be UTF-8") from exc
        if isinstance(source.content, str) and source.content != text:
            raise SkillAnalysisError("SubjectSnapshot content differs from frozen SKILL.md")
        computed = hash_skill_subject(skill_bytes, manifest_bytes, resource_files)
        if source.content_hash != computed:
            raise SkillAnalysisError("SubjectSnapshot content hash mismatch")
        entrypoint = source.metadata.get("entrypoint", source_path)
        return _FrozenSource(text, str(entrypoint), _normalized_subject_hash(computed))
    string_path = None  # type: Optional[Path]
    if isinstance(source, str) and "\n" not in source and "\r" not in source:
        try:
            candidate = Path(source).expanduser()
            if candidate.exists() or candidate.is_symlink():
                string_path = candidate
        except (OSError, ValueError):
            string_path = None
    if isinstance(source, os.PathLike) or string_path is not None:
        try:
            path_value = string_path if string_path is not None else Path(source)
            snapshot = SkillMarkdownSubjectAdapter().snapshot(str(path_value))
        except Exception as exc:
            raise SkillAnalysisError("cannot read frozen Skill path: %s" % exc) from exc
        return _load_frozen_source(snapshot, source_path)
    if isinstance(source, bytes):
        raw = source
    elif isinstance(source, str):
        raw = source.encode("utf-8")
    else:
        raise SkillAnalysisError(
            "source must be Markdown text, UTF-8 bytes, Path, or SubjectSnapshot"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillAnalysisError("SKILL.md must be UTF-8") from exc
    return _FrozenSource(text, source_path, _sha256(raw))


def _headings(lines: Sequence[str]) -> Tuple[_Heading, ...]:
    starts = []  # type: List[Tuple[str, int, int]]
    in_fence = False
    for index, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line.rstrip("\r\n"))
        if match:
            starts.append((match.group(2).strip(), len(match.group(1)), index))
    result = []
    for index, (title, level, line) in enumerate(starts):
        end_line = len(lines)
        for _, next_level, next_line in starts[index + 1 :]:
            if next_level <= level:
                end_line = next_line - 1
                break
        result.append(_Heading(title, level, line, max(line, end_line)))
    return tuple(result)


def _line_value(line: str) -> str:
    stripped = line.strip()
    bullet = _BULLET.match(stripped)
    return (bullet.group(1) if bullet else stripped).strip()


def _dedupe(values: Iterable[str]) -> Tuple[str, ...]:
    result = []
    seen = set()
    for value in values:
        normalized = value.strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
    return tuple(result)


def _tool_kind_and_capabilities(name: str) -> Tuple[str, Tuple[str, ...]]:
    lowered = name.lower()
    if lowered in ("list_files", "read_file"):
        return "workspace", ("workspace_fixture", "canonical_trace")
    if lowered == "write_file":
        return "workspace", (
            "workspace_fixture",
            "artifact_output",
            "canonical_trace",
        )
    if lowered == "browser" or lowered.startswith("browser_"):
        return "browser", ("browser", "canonical_trace")
    if lowered in ("curl", "wget"):
        return "network_cli", ("process_exec", "network", "canonical_trace")
    if lowered in ("process_exec", "shell"):
        return "process", ("process_exec", "canonical_trace")
    return "cli", ("process_exec", "canonical_trace")


def _extract_tools(
    lines: Sequence[str], source_path: str
) -> Tuple[ToolReference, ...]:
    occurrences = {}  # type: Dict[str, List[SourceRef]]
    in_shell_fence = False
    tool_ranges = tuple(
        (heading.line, heading.end_line)
        for heading in _headings(lines)
        if heading.title.strip().casefold() in ("tool", "tools", "工具", "cli")
    )
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            language = stripped[3:].strip().lower()
            if in_shell_fence:
                in_shell_fence = False
            else:
                in_shell_fence = language in ("sh", "bash", "shell", "console", "zsh")
            continue
        candidates = []  # type: List[str]
        in_tool_section = any(start <= line_number <= end for start, end in tool_ranges)
        lowered = line.lower()
        for known in _KNOWN_TOOLS:
            distinctive = known in (
                "list_files",
                "read_file",
                "write_file",
                "process_exec",
                "shell",
                "browser",
            )
            if (
                distinctive or _TOOL_CONTEXT.search(line) or in_tool_section or in_shell_fence
            ) and re.search(
                r"(?<![A-Za-z0-9_.-])%s(?![A-Za-z0-9_.-])" % re.escape(known),
                lowered,
            ):
                candidates.append(known)
        if _TOOL_CONTEXT.search(line) or in_tool_section:
            for quoted in _BACKTICK.findall(line):
                token = quoted.strip().split()[0] if quoted.strip() else ""
                if _looks_like_tool_token(token):
                    candidates.append(token)
        action_tool = re.search(
            r"\b(?:use|call|invoke|run|execute)\s+`?([A-Za-z][A-Za-z0-9_.-]*)`?|"
            r"(?:使用|调用|运行|执行)\s*`?([A-Za-z][A-Za-z0-9_.-]*)`?",
            line,
            re.IGNORECASE,
        )
        if action_tool:
            token = action_tool.group(1) or action_tool.group(2) or ""
            if _looks_like_tool_token(token):
                candidates.append(token)
        if in_shell_fence and stripped and not stripped.startswith(("#", "$")):
            command = stripped.lstrip("$ ").split()[0]
            if _looks_like_tool_token(command):
                candidates.append(command)
        if not candidates:
            continue
        ref = _source_ref(lines, source_path, line_number, line_number, "explicit")
        for candidate in candidates:
            occurrences.setdefault(candidate, []).append(ref)
    tools = []
    for name in sorted(occurrences, key=lambda item: item.casefold()):
        kind, capabilities = _tool_kind_and_capabilities(name)
        refs = []
        seen_refs = set()
        for ref in occurrences[name]:
            key = (ref.start_line, ref.end_line)
            if key not in seen_refs:
                refs.append(ref)
                seen_refs.add(key)
        tools.append(ToolReference(name, kind, capabilities, tuple(refs)))
    return tuple(tools)


def _looks_like_tool_token(value: str) -> bool:
    if not _SIMPLE_TOOL.match(value):
        return False
    lowered = value.lower()
    if lowered.endswith(
        (".json", ".csv", ".txt", ".md", ".yaml", ".yml", ".xml", ".html", ".py", ".js", ".ts")
    ):
        return False
    return lowered not in ("input", "output", "file", "files", "result")


def _capability_ranges(
    lines: Sequence[str], headings: Sequence[_Heading]
) -> Tuple[Tuple[str, int, int, bool], ...]:
    candidates = []  # type: List[Tuple[str, int, int, bool]]
    capability_containers = {
        heading.line
        for heading in headings
        if heading.title.strip().casefold()
        in ("capability", "capabilities", "能力", "功能")
    }
    for container_line in sorted(capability_containers):
        container = next(item for item in headings if item.line == container_line)
        child_headings = [
            item
            for item in headings
            if container.line < item.line <= container.end_line
            and item.level == container.level + 1
        ]
        if child_headings:
            for child in child_headings:
                candidates.append((child.title, child.line, child.end_line, False))
        else:
            for line_number in range(container.line + 1, container.end_line + 1):
                match = _BULLET.match(lines[line_number - 1].rstrip("\r\n"))
                if match and _ACTION.search(match.group(1)):
                    candidates.append((match.group(1).strip(), line_number, line_number, False))
    for heading in headings:
        normalized = heading.title.strip().casefold()
        if heading.level == 1 or normalized in _GENERIC_HEADINGS:
            continue
        if any(
            start <= heading.line <= end
            for _, start, end, _ in candidates
        ):
            continue
        body = "".join(lines[heading.line - 1 : heading.end_line])
        if _ACTION.search(heading.title) or _ACTION.search(body):
            candidates.append((heading.title, heading.line, heading.end_line, False))
    if not candidates:
        root = next((item for item in headings if item.level == 1), None)
        if root is not None:
            candidates.append((root.title, root.line, root.end_line, False))
        else:
            title, first = _fallback_title(lines, headings)
            candidates.append((title[:120], first, max(first, len(lines)), True))
    ordered = sorted(candidates, key=lambda item: (item[1], item[2], item[0]))
    deduped = []
    seen = set()
    for item in ordered:
        key = (item[0].casefold(), item[1], item[2])
        if key not in seen:
            deduped.append(item)
            seen.add(key)
    return tuple(deduped)


def _fallback_title(
    lines: Sequence[str], headings: Sequence[_Heading]
) -> Tuple[str, int]:
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            stripped = lines[index].strip()
            if stripped == "---":
                break
            match = re.match(r"name\s*:\s*(.+?)\s*$", stripped, re.IGNORECASE)
            if match and match.group(1).strip():
                return match.group(1).strip().strip("\"'"), 1
    if headings:
        return headings[0].title, headings[0].line
    first = next((index for index, line in enumerate(lines, 1) if line.strip()), 1)
    return _line_value(lines[first - 1]) or "Skill behavior", first


def _labeled_values(
    lines: Sequence[str], start: int, end: int, labels: Iterable[str]
) -> Tuple[str, ...]:
    accepted = {item.casefold() for item in labels}
    values = []
    active_heading = None  # type: Optional[str]
    active_level = 7
    for line_number in range(start, end + 1):
        raw = lines[line_number - 1].rstrip("\r\n")
        heading = _HEADING.match(raw)
        if heading:
            title = heading.group(2).strip().casefold()
            level = len(heading.group(1))
            if title in accepted:
                active_heading = title
                active_level = level
            elif active_heading is not None and level <= active_level:
                active_heading = None
            continue
        match = _LABEL.match(raw)
        if match and match.group(1).casefold() in accepted:
            values.extend(
                item.strip().strip("`")
                for item in re.split(r"\s*[,，;；]\s*", match.group(2))
                if item.strip()
            )
            continue
        if active_heading is not None:
            bullet = _BULLET.match(raw)
            if bullet:
                values.append(bullet.group(1).strip().strip("`"))
    return _dedupe(values)


def _implicit_io(
    lines: Sequence[str], start: int, end: int, output: bool
) -> Tuple[str, ...]:
    if output:
        action = re.compile(
            r"\b(?:write|create|save|produce|return|output)\b|写入|创建|保存|生成|返回|输出",
            re.I,
        )
    else:
        action = re.compile(r"\b(?:read|load|input|given)\b|读取|加载|输入|给定", re.I)
    values = []
    for line_number in range(start, end + 1):
        line = lines[line_number - 1]
        if not action.search(line):
            continue
        quoted = _BACKTICK.findall(line)
        file_names = re.findall(
            r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_-]+\.(?:json|csv|txt|md|yaml|yml|xml|html|py|js|ts))(?![A-Za-z0-9_.-])",
            line,
            re.I,
        )
        values.extend(
            item.strip()
            for item in quoted
            if len(item.strip()) <= 160
            and item.strip().lower() not in _KNOWN_TOOLS
            and not _looks_like_tool_token(item.strip())
        )
        values.extend(file_names)
    return _dedupe(values)


def _matching_lines(
    lines: Sequence[str], start: int, end: int, pattern: re.Pattern
) -> Tuple[str, ...]:
    return _dedupe(
        _line_value(lines[index - 1])
        for index in range(start, end + 1)
        if pattern.search(lines[index - 1])
    )


def _side_effect_lines(
    lines: Sequence[str], start: int, end: int
) -> Tuple[str, ...]:
    negated = re.compile(
        r"\b(?:never|must not|do not|don't|shall not)\b|禁止|不得|不可",
        re.IGNORECASE,
    )
    return _dedupe(
        _line_value(lines[index - 1])
        for index in range(start, end + 1)
        if _SIDE_EFFECT.search(lines[index - 1])
        and not negated.search(lines[index - 1])
    )


def _steps(lines: Sequence[str], start: int, end: int) -> Tuple[str, ...]:
    values = []
    workflow_level = None  # type: Optional[int]
    for line_number in range(start, end + 1):
        raw = lines[line_number - 1].rstrip("\r\n")
        heading = _HEADING.match(raw)
        if heading:
            title = heading.group(2).strip().casefold()
            level = len(heading.group(1))
            if title in ("workflow", "steps", "instructions", "工作流", "步骤"):
                workflow_level = level
            elif workflow_level is not None and level <= workflow_level:
                workflow_level = None
            continue
        numbered = _NUMBERED.match(raw)
        if numbered:
            values.append(numbered.group(1).strip())
            continue
        bullet = _BULLET.match(raw)
        if workflow_level is not None and bullet and _ACTION.search(bullet.group(1)):
            values.append(bullet.group(1).strip())
    return _dedupe(values)


def _branches(
    lines: Sequence[str], source_path: str, capability_id: str, start: int, end: int
) -> Tuple[CapabilityBranch, ...]:
    values = []
    used = set()
    for line_number in range(start, end + 1):
        line = lines[line_number - 1]
        if not _CONDITIONAL.search(line):
            continue
        condition = _line_value(line)
        branch_id = _unique_id("%s.branch" % capability_id, condition, used)
        values.append(
            CapabilityBranch(
                branch_id,
                condition,
                _source_ref(lines, source_path, line_number, line_number, "explicit"),
            )
        )
    return tuple(values)


def analyze_skill(
    source: Union[str, bytes, os.PathLike, SubjectSnapshot],
    *,
    source_path: str = "SKILL.md",
) -> CapabilityGraph:
    """Analyze frozen Markdown text, UTF-8 bytes, a Path, or SubjectSnapshot.

    Existing filesystem paths may be supplied as ``str`` or ``Path``. Other
    ``str`` values are treated as Markdown content, which keeps in-memory use
    convenient without requiring temporary files.
    """

    frozen = _load_frozen_source(source, source_path)
    if not frozen.text.strip():
        raise SkillAnalysisError("SKILL.md must not be empty")
    lines = frozen.text.splitlines(keepends=True)
    headings = _headings(lines)
    section_ids = set()
    sections = []
    for heading in headings:
        sections.append(
            SkillSection(
                _unique_id("section", heading.title, section_ids),
                heading.title,
                heading.level,
                _source_ref(
                    lines,
                    frozen.source_path,
                    heading.line,
                    heading.end_line,
                    "explicit",
                ),
            )
        )
    if not sections:
        sections.append(
            SkillSection(
                "section.document",
                "Document",
                0,
                _source_ref(
                    lines, frozen.source_path, 1, len(lines), "explicit"
                ),
            )
        )

    tools = _extract_tools(lines, frozen.source_path)
    capability_ids = set()
    capabilities = []
    for name, start, end, inferred in _capability_ranges(lines, headings):
        capability_id = _unique_id("cap", name, capability_ids)
        inputs = _dedupe(
            _labeled_values(lines, start, end, ("input", "inputs", "输入"))
            + _implicit_io(lines, start, end, False)
        )
        outputs = _dedupe(
            _labeled_values(lines, start, end, ("output", "outputs", "输出"))
            + _implicit_io(lines, start, end, True)
        )
        preconditions = _labeled_values(
            lines,
            start,
            end,
            ("precondition", "preconditions", "前置条件", "requirements", "要求"),
        )
        capability_tools = tuple(
            tool.name
            for tool in tools
            if any(start <= ref.start_line <= end for ref in tool.source_refs)
        )
        capabilities.append(
            Capability(
                id=capability_id,
                name=name,
                source_refs=(
                    _source_ref(
                        lines,
                        frozen.source_path,
                        start,
                        end,
                        "inferred" if inferred else "explicit",
                    ),
                ),
                inputs=inputs,
                outputs=outputs,
                preconditions=preconditions,
                steps=_steps(lines, start, end),
                branches=_branches(
                    lines, frozen.source_path, capability_id, start, end
                ),
                tools=capability_tools,
                state_transitions=_matching_lines(lines, start, end, _STATE),
                side_effects=_side_effect_lines(lines, start, end),
                risks=_matching_lines(lines, start, end, _RISK),
                inferred=inferred,
            )
        )

    ambiguities = []
    ambiguity_ids = set()
    for line_number, line in enumerate(lines, 1):
        match = _VAGUE.search(line)
        if not match:
            continue
        text = _line_value(line)
        reason = (
            "explicit_placeholder"
            if _EXPLICIT_PLACEHOLDER.search(line)
            else "subjective_or_vague"
        )
        containing = next(
            (
                capability
                for capability in capabilities
                if capability.source_refs[0].start_line
                <= line_number
                <= capability.source_refs[0].end_line
            ),
            None,
        )
        ambiguities.append(
            SkillAmbiguity(
                _unique_id("ambiguity", "%s-%d" % (reason, line_number), ambiguity_ids),
                reason,
                text,
                _source_ref(
                    lines,
                    frozen.source_path,
                    line_number,
                    line_number,
                    "explicit",
                ),
                containing.id if containing is not None else None,
            )
        )
    assigned_tools = {
        tool_name for capability in capabilities for tool_name in capability.tools
    }
    for tool in tools:
        if tool.name in assigned_tools:
            continue
        original = tool.source_refs[0]
        ambiguities.append(
            SkillAmbiguity(
                _unique_id(
                    "ambiguity",
                    "unassigned-tool-%s" % tool.name,
                    ambiguity_ids,
                ),
                "unassigned_tool_dependency",
                "Declared tool %s cannot be assigned to a specific capability."
                % tool.name,
                SourceRef(
                    original.path,
                    original.start_line,
                    original.end_line,
                    original.quote_sha256,
                    "inferred",
                ),
                None,
            )
        )
    for capability in capabilities:
        if capability.outputs:
            continue
        original = capability.source_refs[0]
        inferred_ref = SourceRef(
            original.path,
            original.start_line,
            original.end_line,
            original.quote_sha256,
            "inferred",
        )
        ambiguities.append(
            SkillAmbiguity(
                _unique_id(
                    "ambiguity",
                    "%s-missing-output" % capability.id,
                    ambiguity_ids,
                ),
                "missing_declared_output",
                "Capability does not declare a machine-observable output.",
                inferred_ref,
                capability.id,
            )
        )

    graph = CapabilityGraph(
        subject_hash=frozen.subject_hash,
        source_path=frozen.source_path,
        sections=tuple(sections),
        capabilities=tuple(capabilities),
        tools=tools,
        ambiguities=tuple(ambiguities),
    )
    graph.validate_source(frozen.text)
    return graph


class SkillAnalyzer:
    """Small object-oriented facade for dependency injection and services."""

    id = "deterministic-skill-analyzer/v1"

    def analyze(
        self,
        source: Union[str, bytes, os.PathLike, SubjectSnapshot],
        *,
        source_path: str = "SKILL.md",
    ) -> CapabilityGraph:
        return analyze_skill(source, source_path=source_path)


__all__ = [
    "SKILL_ANALYSIS_API_VERSION",
    "SOURCE_BINDINGS",
    "Capability",
    "CapabilityBranch",
    "CapabilityGraph",
    "SkillAmbiguity",
    "SkillAnalysisError",
    "SkillAnalyzer",
    "SkillSection",
    "SourceRef",
    "ToolReference",
    "analyze_skill",
]
