"""Subject-neutral boundary shared by Skill and Agent evaluation.

The current optimizer mutates Skill resources, while the evaluation Kernel
operates on this smaller immutable subject identity.  Agent optimization can
therefore reuse Case/Path generation, remote execution, evidence grading,
paired comparison, and convergence without pretending an Agent is a file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from .contracts import SubjectSnapshot
from .environment_contracts import canonical_hash


EVALUATION_SUBJECT_API_VERSION = "aceval.evaluation-subject/v1"
SUBJECT_KINDS = frozenset(("skill", "agent"))


class SubjectContractError(ValueError):
    pass


class SubjectValidationError(ValueError):
    """An existing file-backed Subject is unsafe or inconsistent."""


def _read_utf8(path: Path, label: str) -> Tuple[bytes, str]:
    try:
        raw = path.read_bytes()
        return raw, raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SubjectValidationError("invalid %s: %s" % (label, exc)) from exc


def _reject_json_constant(value: str) -> Any:
    raise ValueError("non-standard JSON constant: %s" % value)


def _load_subject_manifest(path: Path) -> Tuple[bytes, Dict[str, Any]]:
    raw, text = _read_utf8(path, "subject.json")
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SubjectValidationError("invalid subject.json: %s" % exc) from exc
    if not isinstance(value, dict):
        raise SubjectValidationError("subject.json must contain a JSON object")
    manifest_metadata = value.get("metadata", {})
    if not isinstance(manifest_metadata, dict):
        raise SubjectValidationError("subject.json metadata must be a JSON object")
    return raw, value


_RESOURCE_DIRECTORIES = frozenset(
    ("scripts", "references", "templates", "workflow", "knowledge", "specs", "config", "assets")
)
_RESOURCE_EXCLUDED_PARTS = frozenset((".git", "__pycache__", "node_modules", "dist", "build"))
_RESOURCE_LINK = re.compile(r"\]\(([^)#?]+)(?:[?#][^)]*)?\)|`([^`\n]+\.[A-Za-z0-9]{1,12})`")
_MAX_RESOURCE_FILES = 2048
_MAX_RESOURCE_BYTES = 32 * 1024 * 1024


def _safe_resource_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in ("", ".", "..") for part in path.parts)
        or any(part in _RESOURCE_EXCLUDED_PARTS for part in path.parts)
        or path.name == ".env"
        or path.name.startswith(".env.")
    ):
        raise SubjectValidationError("unsafe Skill resource path: %s" % value)
    return value


def _resolve_resource_reference(base: PurePosixPath, reference: str) -> str:
    """Normalize a resource-relative reference without allowing root escape."""

    if not reference or "\x00" in reference or "\\" in reference:
        raise SubjectValidationError("unsafe Skill resource reference: %s" % reference)
    raw = PurePosixPath(reference)
    if raw.is_absolute():
        raise SubjectValidationError("unsafe Skill resource reference: %s" % reference)
    parts = [part for part in base.parts if part not in ("", ".")]
    for part in raw.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise SubjectValidationError("Skill resource reference escaped its root: %s" % reference)
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise SubjectValidationError("unsafe Skill resource reference: %s" % reference)
    return _safe_resource_path(PurePosixPath(*parts).as_posix())


def _resource_closure(root: Path, skill_text: str) -> Tuple[Dict[str, bytes], Tuple[Mapping[str, str], ...]]:
    """Freeze declared Skill resources without following links or executing them."""

    pending = []
    issues = []
    for directory in sorted(_RESOURCE_DIRECTORIES):
        candidate = root / directory
        if candidate.exists() or candidate.is_symlink():
            pending.append((candidate, None, None))
    for match in _RESOURCE_LINK.finditer(skill_text):
        reference = (match.group(1) or match.group(2) or "").strip()
        if not reference or "://" in reference or reference.startswith(("#", "/")):
            continue
        try:
            relative = _resolve_resource_reference(PurePosixPath(), reference)
        except SubjectValidationError as exc:
            issues.append({"source_path": "SKILL.md", "reference": reference, "reason": str(exc)})
            continue
        pending.append((root / relative, "SKILL.md", reference))

    files: Dict[str, bytes] = {}
    visited = set()
    total = 0
    while pending:
        candidate, referenced_from, original_reference = pending.pop(0)
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError as exc:
            raise SubjectValidationError("Skill resource escaped its root") from exc
        if relative in visited:
            continue
        visited.add(relative)
        _safe_resource_path(relative)
        if candidate.is_symlink():
            raise SubjectValidationError("Skill resource symlink is not allowed: %s" % relative)
        if not candidate.exists():
            if referenced_from is not None:
                issues.append({
                    "source_path": referenced_from,
                    "reference": str(original_reference or relative),
                    "reason": "referenced Skill resource does not exist: %s" % relative,
                })
            continue
        if candidate.is_dir():
            for child in sorted(candidate.iterdir()):
                pending.append((child, None, None))
            continue
        if not candidate.is_file():
            raise SubjectValidationError("Skill resource must be a regular file: %s" % relative)
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            raise SubjectValidationError("cannot read Skill resource: %s" % relative) from exc
        total += len(raw)
        if len(files) + 1 > _MAX_RESOURCE_FILES or total > _MAX_RESOURCE_BYTES:
            raise SubjectValidationError("Skill resource closure exceeds the safety limit")
        files[relative] = raw
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for match in _RESOURCE_LINK.finditer(text):
            reference = (match.group(1) or match.group(2) or "").strip()
            if not reference or "://" in reference or reference.startswith(("#", "/")):
                continue
            base = PurePosixPath(relative).parent
            try:
                safe = _resolve_resource_reference(base, reference)
            except SubjectValidationError as exc:
                issues.append({"source_path": relative, "reference": reference, "reason": str(exc)})
                continue
            pending.append((root / safe, relative, reference))
    unique_issues = {
        (item["source_path"], item["reference"], item["reason"]): item
        for item in issues
    }
    return files, tuple(unique_issues[key] for key in sorted(unique_issues))


def hash_skill_subject(
    skill_bytes: bytes,
    manifest_bytes: Optional[bytes],
    resource_files: Optional[Mapping[str, bytes]] = None,
) -> str:
    resources = {
        _safe_resource_path(str(path)): content
        for path, content in dict(resource_files or {}).items()
        if str(path) not in ("SKILL.md", "subject.json")
    }
    if any(not isinstance(content, bytes) for content in resources.values()):
        raise SubjectValidationError("frozen Skill resources must contain bytes")
    if resources:
        digest = hashlib.sha256()
        digest.update(b"aceval-skill-subject-v2\0")
        all_files = {"SKILL.md": skill_bytes, **resources}
        if manifest_bytes is not None:
            all_files["subject.json"] = manifest_bytes
        for path in sorted(all_files):
            name = path.encode("utf-8")
            content = all_files[path]
            digest.update(len(name).to_bytes(4, "big"))
            digest.update(name)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()
    if manifest_bytes is None:
        return hashlib.sha256(skill_bytes).hexdigest()
    digest = hashlib.sha256()
    digest.update(b"aceval-skill-subject-v1\0")
    for name, content in ((b"SKILL.md", skill_bytes), (b"subject.json", manifest_bytes)):
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _has_value(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value))


def _is_generated_candidate(root: Path, content_hash: str) -> bool:
    marker = root / "candidate.json"
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        _, text = _read_utf8(marker, "candidate.json")
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (SubjectValidationError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(value, dict) and value.get("subject_hash") == content_hash


class SkillMarkdownSubjectAdapter:
    """Backward-compatible frozen Subject adapter used by EvalPack V1."""

    id = "skill_markdown_v1"

    def snapshot(self, subject_ref: str, params: Optional[Mapping[str, Any]] = None) -> SubjectSnapshot:
        del params
        source = Path(subject_ref).expanduser()
        if source.is_symlink():
            raise SubjectValidationError("subject symlink is not allowed")
        directory_mode = source.is_dir()
        skill_file = source / "SKILL.md" if directory_mode else source
        # A direct ``.../SKILL.md`` reference addresses the same Skill
        # checkout as its parent directory.  Keep the resource closure and
        # optional subject manifest identical for both forms; otherwise the
        # evaluation compiler could freeze one hash while the runtime binds
        # the other (especially when ``references/`` or ``scripts/`` exist).
        if not directory_mode and source.name == "SKILL.md":
            directory_mode = True
        if skill_file.is_symlink():
            raise SubjectValidationError("SKILL.md symlink is not allowed")
        if not skill_file.is_file():
            raise SubjectValidationError("SKILL.md not found: %s" % skill_file)
        skill_file = skill_file.resolve()
        skill_bytes, content = _read_utf8(skill_file, "SKILL.md")
        root = skill_file.parent
        manifest: Dict[str, Any] = {}
        manifest_bytes: Optional[bytes] = None
        if directory_mode:
            manifest_file = root / "subject.json"
            if manifest_file.is_symlink():
                raise SubjectValidationError("subject.json symlink is not allowed")
            if manifest_file.exists():
                if not manifest_file.is_file():
                    raise SubjectValidationError("subject.json must be a regular file")
                manifest_bytes, manifest = _load_subject_manifest(manifest_file)
        if directory_mode:
            resource_files, resource_issues = _resource_closure(root, content)
        else:
            resource_files, resource_issues = {}, ()
        manifest_metadata = manifest.get("metadata", {})
        metadata = dict(manifest_metadata)
        for key in ("id", "version"):
            if key in manifest and key not in metadata:
                metadata[key] = manifest[key]
        content_hash = hash_skill_subject(skill_bytes, manifest_bytes, resource_files)
        variant = metadata.get("variant")
        if directory_mode and _is_generated_candidate(root, content_hash):
            variant = "candidate"
        if not _has_value(variant):
            variant = metadata.get("version")
        if not _has_value(variant):
            variant = root.name
        metadata.update({
            "path": str(root),
            "entrypoint": "SKILL.md",
            "variant": variant,
            "resource_paths": sorted(resource_files),
            "resource_count": len(resource_files),
            "resource_issues": [dict(item) for item in resource_issues],
        })
        return SubjectSnapshot(
            kind="skill", uri=str(root), content_hash=content_hash, content=content,
            files={
                key: value
                for key, value in (
                    ("SKILL.md", skill_bytes),
                    ("subject.json", manifest_bytes),
                    *tuple(sorted(resource_files.items())),
                )
                if value is not None
            },
            metadata=metadata,
        )

    def materialize(self, snapshot: SubjectSnapshot, destination: Path, params: Optional[Mapping[str, Any]] = None) -> Path:
        destination = destination.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise SubjectValidationError("subject destination is not empty")
        frozen_files = dict(snapshot.files)
        if not frozen_files:
            if isinstance(snapshot.content, str):
                frozen_files = {"SKILL.md": snapshot.content.encode("utf-8")}
            else:
                source_snapshot = self.snapshot(snapshot.uri, params)
                if source_snapshot.content_hash != snapshot.content_hash:
                    raise SubjectValidationError("Subject source changed after snapshot")
                frozen_files = dict(source_snapshot.files)
        skill_bytes = frozen_files.get("SKILL.md")
        manifest_bytes = frozen_files.get("subject.json")
        resource_files = {
            _safe_resource_path(str(path)): content
            for path, content in frozen_files.items()
            if path not in ("SKILL.md", "subject.json")
        }
        if (
            not isinstance(skill_bytes, bytes)
            or (manifest_bytes is not None and not isinstance(manifest_bytes, bytes))
            or any(not isinstance(content, bytes) for content in resource_files.values())
        ):
            raise SubjectValidationError("frozen Subject files must contain bytes")
        try:
            skill_content = skill_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SubjectValidationError("invalid frozen SKILL.md: %s" % exc) from exc
        if isinstance(snapshot.content, str) and skill_content != snapshot.content:
            raise SubjectValidationError("frozen SKILL.md differs from snapshot content")
        if snapshot.content_hash != hash_skill_subject(skill_bytes, manifest_bytes, resource_files):
            raise SubjectValidationError("frozen Subject content hash mismatch")
        for name, value in sorted(frozen_files.items()):
            _safe_resource_path(name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
            target.chmod(0o444)
        if snapshot.metadata.get("variant") == "candidate":
            marker = destination / "candidate.json"
            marker.write_text(json.dumps({"contract": "aceval.selected-candidate/v1", "subject_hash": snapshot.content_hash}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
            marker.chmod(0o444)
        return destination


def with_variant(snapshot: SubjectSnapshot, variant: str) -> SubjectSnapshot:
    metadata = dict(snapshot.metadata)
    metadata["variant"] = variant
    return SubjectSnapshot(
        kind=snapshot.kind, uri=snapshot.uri, content_hash=snapshot.content_hash,
        parent_hash=snapshot.parent_hash, content=snapshot.content, files=snapshot.files, metadata=metadata,
    )


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum or "\x00" in value:
        raise SubjectContractError("%s must be trimmed non-empty text" % label)
    return value


def _strict(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise SubjectContractError("%s must be strict JSON" % label) from exc


@dataclass(frozen=True)
class EvaluationSubject:
    kind: str
    subject_id: str
    revision: str
    entrypoint: str
    capabilities: Tuple[str, ...] = ()
    resource_refs: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    api_version: str = EVALUATION_SUBJECT_API_VERSION

    def __post_init__(self) -> None:
        if self.api_version != EVALUATION_SUBJECT_API_VERSION or self.kind not in SUBJECT_KINDS:
            raise SubjectContractError("unsupported evaluation subject")
        _text(self.subject_id, "subject_id", 512)
        _text(self.revision, "revision", 512)
        _text(self.entrypoint, "entrypoint", 4096)
        object.__setattr__(self, "capabilities", tuple(_text(item, "capabilities[]", 512) for item in self.capabilities))
        object.__setattr__(self, "resource_refs", tuple(_text(item, "resource_refs[]", 4096) for item in self.resource_refs))
        object.__setattr__(self, "metadata", _strict(self.metadata, "metadata"))

    @property
    def fingerprint(self) -> str:
        return canonical_hash(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> Mapping[str, Any]:
        value = {
            "api_version": self.api_version,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "revision": self.revision,
            "entrypoint": self.entrypoint,
            "capabilities": list(self.capabilities),
            "resource_refs": list(self.resource_refs),
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_mapping(cls, value: Any) -> "EvaluationSubject":
        if not isinstance(value, Mapping):
            raise SubjectContractError("evaluation subject must be an object")
        allowed = {"api_version", "kind", "subject_id", "revision", "entrypoint", "capabilities", "resource_refs", "metadata", "fingerprint"}
        unknown = sorted(set(value).difference(allowed))
        if unknown:
            raise SubjectContractError("unknown subject fields: %s" % ", ".join(unknown))
        subject = cls(
            api_version=value.get("api_version", ""),
            kind=value.get("kind", ""),
            subject_id=value.get("subject_id", ""),
            revision=value.get("revision", ""),
            entrypoint=value.get("entrypoint", ""),
            capabilities=tuple(value.get("capabilities", ())),
            resource_refs=tuple(value.get("resource_refs", ())),
            metadata=value.get("metadata", {}),
        )
        if value.get("fingerprint") is not None and value.get("fingerprint") != subject.fingerprint:
            raise SubjectContractError("evaluation subject fingerprint mismatch")
        return subject


def skill_subject(*, repository: str, revision: str, entrypoint: str = "SKILL.md", resource_refs: Tuple[str, ...] = ()) -> EvaluationSubject:
    return EvaluationSubject(kind="skill", subject_id=repository, revision=revision, entrypoint=entrypoint, resource_refs=resource_refs)


def agent_subject(
    *,
    agent_id: str,
    revision: str,
    environment_id: str,
    model_id: str,
    tool_manifest_hash: str,
    policy_refs: Tuple[str, ...] = (),
) -> EvaluationSubject:
    return EvaluationSubject(
        kind="agent",
        subject_id=agent_id,
        revision=revision,
        entrypoint="agent-session",
        resource_refs=policy_refs,
        metadata={"environment_id": environment_id, "model_id": model_id, "tool_manifest_hash": tool_manifest_hash},
    )


__all__ = [
    "EVALUATION_SUBJECT_API_VERSION", "SUBJECT_KINDS", "EvaluationSubject",
    "SkillMarkdownSubjectAdapter", "SubjectContractError", "SubjectValidationError",
    "agent_subject", "hash_skill_subject", "skill_subject", "with_variant",
]
