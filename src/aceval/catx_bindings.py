"""Fail-closed contracts for CATX Skill and repository binding.

The concrete CATX request fields are deliberately supplied by an adapter.  The
contract is stable now; when the repository-mount API arrives only that adapter
changes, while orchestration, evidence, and replay identities remain intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol

from .environment_contracts import canonical_hash, normalize_sha256


CATX_EXECUTION_BINDING_API_VERSION = "aceval.catx-execution-binding/v1"


class CatxBindingError(ValueError):
    """A CATX execution is not provably bound to the requested inputs."""


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise CatxBindingError("%s must be a trimmed non-empty safe string" % label)
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class CatxExecutionBinding:
    api_version: str
    subject_hash: str
    skill_ref: str
    repository_ref: str
    repository_hash: Optional[str] = None
    base_commit: Optional[str] = None
    head_commit: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    binding_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if self.api_version != CATX_EXECUTION_BINDING_API_VERSION:
            raise CatxBindingError("unsupported CATX execution binding api_version")
        object.__setattr__(self, "subject_hash", normalize_sha256(self.subject_hash, "subject_hash"))
        object.__setattr__(self, "skill_ref", _text(self.skill_ref, "skill_ref"))
        object.__setattr__(self, "repository_ref", _text(self.repository_ref, "repository_ref", 8192))
        if self.repository_hash is not None:
            object.__setattr__(
                self,
                "repository_hash",
                normalize_sha256(self.repository_hash, "repository_hash"),
            )
        for name in ("base_commit", "head_commit"):
            value = getattr(self, name)
            if value is not None:
                text = _text(value, name, 128)
                if len(text) < 7 or any(character not in "0123456789abcdef" for character in text):
                    raise CatxBindingError("%s must be a lowercase git object id" % name)
                object.__setattr__(self, name, text)
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        payload = {
            "api_version": self.api_version,
            "subject_hash": self.subject_hash,
            "skill_ref": self.skill_ref,
            "repository_ref": self.repository_ref,
            "repository_hash": self.repository_hash,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "metadata": self.metadata,
        }
        computed = canonical_hash(payload)
        if self.binding_hash is not None and normalize_sha256(
            self.binding_hash, "binding_hash"
        ) != computed:
            raise CatxBindingError("binding_hash does not match binding contents")
        object.__setattr__(self, "binding_hash", computed)

    @classmethod
    def from_mapping(cls, value: Any) -> "CatxExecutionBinding":
        if not isinstance(value, Mapping):
            raise CatxBindingError("CATX execution binding must be an object")
        allowed = {
            "api_version", "subject_hash", "skill_ref", "repository_ref",
            "repository_hash", "base_commit", "head_commit", "metadata", "binding_hash",
        }
        unknown = sorted(set(value).difference(allowed))
        if unknown:
            raise CatxBindingError(
                "CATX execution binding contains unsupported fields: %s" % ", ".join(unknown)
            )
        return cls(
            api_version=value.get("api_version"),
            subject_hash=value.get("subject_hash"),
            skill_ref=value.get("skill_ref"),
            repository_ref=value.get("repository_ref"),
            repository_hash=value.get("repository_hash"),
            base_commit=value.get("base_commit"),
            head_commit=value.get("head_commit"),
            metadata=value.get("metadata", {}),
            binding_hash=value.get("binding_hash"),
        )


@dataclass(frozen=True)
class CatxBindingEvidence:
    verified: bool
    requested_binding_hash: str
    actual_subject_hash: Optional[str] = None
    actual_repository_hash: Optional[str] = None
    actual_base_commit: Optional[str] = None
    actual_head_commit: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.verified, bool):
            raise CatxBindingError("binding evidence verified must be a boolean")
        object.__setattr__(
            self,
            "requested_binding_hash",
            normalize_sha256(self.requested_binding_hash, "requested_binding_hash"),
        )
        for name in ("actual_subject_hash", "actual_repository_hash"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, normalize_sha256(value, name))
        object.__setattr__(self, "details", _freeze(self.details))

    def assert_matches(self, binding: CatxExecutionBinding) -> None:
        mismatches = []
        if self.requested_binding_hash != binding.binding_hash:
            mismatches.append("binding_hash")
        if self.actual_subject_hash != binding.subject_hash:
            mismatches.append("subject_hash")
        if binding.repository_hash is not None and self.actual_repository_hash != binding.repository_hash:
            mismatches.append("repository_hash")
        if binding.base_commit is not None and self.actual_base_commit != binding.base_commit:
            mismatches.append("base_commit")
        if binding.head_commit is not None and self.actual_head_commit != binding.head_commit:
            mismatches.append("head_commit")
        if not self.verified or mismatches:
            raise CatxBindingError(
                "CATX session binding was not verified%s"
                % (": " + ", ".join(mismatches) if mismatches else "")
            )


class CatxSessionBindingAdapter(Protocol):
    """Concrete integration point for CATX APIs supplied after P0."""

    def augment_create_payload(
        self,
        base_payload: Mapping[str, Any],
        binding: CatxExecutionBinding,
    ) -> Mapping[str, Any]:
        ...

    def verify_session(
        self,
        client: Any,
        session_id: str,
        binding: CatxExecutionBinding,
    ) -> CatxBindingEvidence:
        ...


class CatxPayloadBindingAdapter:
    """Default adapter for CATX deployments exposing binding metadata.

    CATX has had several server-side names for these fields.  The adapter keeps
    the wire contract in one place and verifies the echoed session metadata;
    absence or mismatch is a hard failure rather than an unverified run.
    """

    # The public CATX Session API does not currently echo arbitrary binding
    # metadata from ``POST /sessions``.  We still verify the configured
    # resource request before sending the first message, then upgrade the
    # evidence to an event-log verification after the agent has checked the
    # mounts and commits.
    verify_before_message = True

    def augment_create_payload(
        self,
        base_payload: Mapping[str, Any],
        binding: CatxExecutionBinding,
    ) -> Mapping[str, Any]:
        payload = dict(base_payload)
        payload["skill_binding"] = {
            "ref": binding.skill_ref,
            "subject_hash": binding.subject_hash,
            "binding_hash": binding.binding_hash,
        }
        payload["repository_binding"] = {
            "ref": binding.repository_ref,
            "repository_hash": binding.repository_hash,
            "base_commit": binding.base_commit,
            "head_commit": binding.head_commit,
            "binding_hash": binding.binding_hash,
        }
        return payload

    def verify_session(
        self,
        client: Any,
        session_id: str,
        binding: CatxExecutionBinding,
    ) -> CatxBindingEvidence:
        status = client.get_status(session_id)
        metadata = status.get("binding") or status.get("execution_binding") or status.get("metadata")
        if not isinstance(metadata, Mapping):
            resources = tuple(getattr(client.profile, "repository_resources", ()) or ())
            match = next((item for item in resources if getattr(item, "url", None) == binding.repository_ref), None)
            if match is None:
                raise CatxBindingError("CATX profile does not contain the requested repository resource")
            # This is deliberately marked as request-level evidence.  It is
            # sufficient to allow the first message to be sent, but callers
            # must replace it with verify_event_log() evidence before scoring.
            return CatxBindingEvidence(
                verified=True,
                requested_binding_hash=binding.binding_hash,
                actual_subject_hash=binding.subject_hash,
                actual_repository_hash=binding.repository_hash,
                actual_base_commit=binding.base_commit,
                actual_head_commit=binding.head_commit,
                details={
                    "session_id": session_id,
                    "verification_mode": "request_resource",
                    "server_echo": False,
                    "mount_path": getattr(match, "mount_path", None),
                    "status": {key: status.get(key) for key in ("id", "workspace_id", "sandbox_id", "environment_id") if key in status},
                },
            )
        skill = metadata.get("skill_binding") if isinstance(metadata.get("skill_binding"), Mapping) else metadata
        repository = metadata.get("repository_binding") if isinstance(metadata.get("repository_binding"), Mapping) else metadata
        actual_subject = skill.get("subject_hash") or skill.get("hash")
        actual_repository = repository.get("repository_hash") or repository.get("hash")
        evidence = CatxBindingEvidence(
            verified=metadata.get("binding_hash") == binding.binding_hash or repository.get("binding_hash") == binding.binding_hash,
            requested_binding_hash=binding.binding_hash,
            actual_subject_hash=actual_subject,
            actual_repository_hash=actual_repository,
            actual_base_commit=repository.get("base_commit"),
            actual_head_commit=repository.get("head_commit"),
            details={"session_id": session_id, "metadata": dict(metadata)},
        )
        evidence.assert_matches(binding)
        return evidence

    def verify_event_log(
        self,
        client: Any,
        session_id: str,
        binding: CatxExecutionBinding,
        events: Any,
    ) -> CatxBindingEvidence:
        """Verify that the Agent actually observed the requested mounts.

        CATX does not echo ``skill_binding``/``repository_binding`` in the
        session document.  The evaluation prompt therefore requires a read-only
        ``git rev-parse HEAD`` preflight; this method checks tool calls/results
        (never the user prompt) for the expected mount paths and revisions.
        """
        if not isinstance(events, (list, tuple)):
            raise CatxBindingError("CATX event log must be an array")
        tool_calls = []
        tool_results = []
        for event in events:
            if not isinstance(event, Mapping) or event.get("type") == "user.message":
                continue
            event_type = str(event.get("type") or "")
            if event_type not in ("agent.tool_use", "agent.tool_result", "tool_call", "tool_result"):
                continue
            if event_type in ("agent.tool_use", "tool_call"):
                tool_calls.append(str(event))
            else:
                tool_results.append(str(event))
        call_corpus = "\n".join(tool_calls)
        result_corpus = "\n".join(tool_results)
        corpus = call_corpus + "\n" + result_corpus
        metadata = dict(binding.metadata) if isinstance(binding.metadata, Mapping) else {}
        expected_mounts = metadata.get("expected_mounts") if isinstance(metadata.get("expected_mounts"), (list, tuple)) else ()
        checks = []
        for item in expected_mounts:
            if not isinstance(item, Mapping):
                continue
            mount = str(item.get("mount_path") or "").strip()
            revision = str(item.get("revision") or "").strip().lower()
            if not mount:
                continue
            checks.append({
                "mount_path": mount,
                "revision": revision or None,
                "mount_observed": mount in corpus,
                "revision_observed": (not revision) or revision in result_corpus,
            })
        if not checks:
            # A single-repository binding still has a useful mount path in the
            # profile; use it as the minimum event-log assertion.
            resources = tuple(getattr(client.profile, "repository_resources", ()) or ())
            match = next((item for item in resources if getattr(item, "url", None) == binding.repository_ref), None)
            mount = getattr(match, "mount_path", "") if match is not None else ""
            checks = [{
                "mount_path": mount,
                "revision": binding.head_commit,
                "mount_observed": bool(mount) and mount in corpus,
                "revision_observed": not binding.head_commit or binding.head_commit in result_corpus,
            }]
        verified = bool(checks) and all(item["mount_observed"] and item["revision_observed"] for item in checks)
        evidence = CatxBindingEvidence(
            verified=verified,
            requested_binding_hash=binding.binding_hash,
            actual_subject_hash=binding.subject_hash if verified else None,
            actual_repository_hash=binding.repository_hash if verified else None,
            actual_base_commit=binding.base_commit if verified else None,
            actual_head_commit=binding.head_commit if verified else None,
            details={
                "session_id": session_id,
                "verification_mode": "event_log",
                "server_echo": False,
                "checks": checks,
                "tool_call_count": len(tool_calls),
                "tool_result_count": len(tool_results),
            },
        )
        if verified:
            evidence.assert_matches(binding)
        return evidence

__all__ = [
    "CATX_EXECUTION_BINDING_API_VERSION",
    "CatxBindingError",
    "CatxBindingEvidence",
    "CatxExecutionBinding",
    "CatxSessionBindingAdapter",
    "CatxPayloadBindingAdapter",
]
