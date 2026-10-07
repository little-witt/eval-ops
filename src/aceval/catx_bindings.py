"""Fail-closed contracts for CATX Skill and repository binding.

The concrete CATX request fields are deliberately supplied by an adapter.  The
contract is stable now; when the repository-mount API arrives only that adapter
changes, while orchestration, evidence, and replay identities remain intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import shlex
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol, Sequence

from .environment_contracts import canonical_hash, normalize_sha256


CATX_EXECUTION_BINDING_API_VERSION = "aceval.catx-execution-binding/v1"

_TOOL_CALL_TYPES = frozenset(("agent.tool_use", "tool.call", "tool_call"))
_TOOL_RESULT_TYPES = frozenset(("agent.tool_result", "tool.result", "tool_result"))
_SUCCESS_STATUSES = frozenset(("ok", "success", "succeeded", "completed", "complete", "done"))
_FULL_SHA = re.compile(r"(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])", re.IGNORECASE)
_COMMAND_TOOL_NAMES = frozenset(
    (
        "bash",
        "command",
        "exec",
        "execute",
        "process_exec",
        "run_command",
        "shell",
        "terminal",
    )
)


class CatxBindingError(ValueError):
    """A CATX execution is not provably bound to the requested inputs."""


def _nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _event_kind(event: Mapping[str, Any]) -> str:
    value = event.get("type") or event.get("kind")
    return str(value or "")


def _call_id(event: Mapping[str, Any], *, result: bool = False) -> Optional[str]:
    keys = (
        ("tool_use_id", "tool_call_id", "call_id", "parent_event_id")
        if result
        else ("id", "tool_call_id", "call_id", "tool_use_id")
    )
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # A few CATX proxies wrap the identifiers in a payload object.  Keep the
    # accepted aliases narrow so arbitrary text fields can never become a
    # binding relationship.
    for container in (event.get("payload"), event.get("data"), event.get("metadata")):
        if not isinstance(container, Mapping):
            continue
        for key in keys:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _command_values(value: Any) -> tuple[str, ...]:
    """Extract executable command fields, never free-form event text."""

    values: list[str] = []

    def visit(item: Any, *, field: Optional[str] = None) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key).casefold()
                if key_text in {"command", "cmd", "shell_command"}:
                    if isinstance(child, str):
                        values.append(child)
                    elif isinstance(child, Sequence) and not isinstance(child, (str, bytes)):
                        if all(isinstance(part, (str, int, float)) and not isinstance(part, bool) for part in child):
                            values.append(" ".join(str(part) for part in child))
                elif key_text in {"argv", "args", "arguments", "cmd"} and isinstance(child, Sequence) and not isinstance(child, (str, bytes)):
                    if all(isinstance(part, (str, int, float)) and not isinstance(part, bool) for part in child):
                        values.append(" ".join(str(part) for part in child))
                    else:
                        visit(child, field=key_text)
                elif key_text in {"input", "parameters", "payload", "data", "arguments", "request"}:
                    visit(child, field=key_text)
        elif field in {"input", "parameters", "payload", "data", "arguments", "request"}:
            if isinstance(item, str) and item.strip():
                values.append(item)
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                for child in item:
                    visit(child, field=field)

    visit(value)
    return tuple(item for item in values if item.strip())


def _result_values(value: Any) -> tuple[str, ...]:
    """Extract structured stdout/result text from a tool result."""

    values: list[str] = []

    def visit(item: Any, *, key: Optional[str] = None) -> None:
        if isinstance(item, str):
            if key in {"text", "stdout", "output", "result", "value", "content", "message", "data"}:
                values.append(item)
            return
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                name = str(child_key).casefold()
                if name in {"text", "stdout", "output", "result", "value", "content", "message", "data"}:
                    visit(child, key=name)
                elif name in {"payload", "data", "result", "response", "output"}:
                    visit(child, key=name)
                # Do not recurse through arbitrary keys: a prose field such
                # as ``description`` must not be accepted as command output.
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            for child in item:
                visit(child, key=key)

    visit(value)
    return tuple(item for item in values if item.strip())


def _command_mount(command: str) -> Optional[str]:
    """Return the mount from a structured ``git -C MOUNT rev-parse HEAD``."""

    try:
        tokens = shlex.split(command, posix=True)
    except (ValueError, TypeError):
        return None
    if len(tokens) != 5 or os.path.basename(tokens[0]) != "git":
        return None
    if tokens[1] != "-C" or tokens[3] != "rev-parse" or tokens[4] != "HEAD":
        return None
    mount = tokens[2]
    if mount.startswith("/"):
        return os.path.normpath(mount)
    return None


def _structured_call(event: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(event, Mapping) or _event_kind(event) not in _TOOL_CALL_TYPES:
        return None
    identifier = _call_id(event)
    commands = _command_values(event)
    if not identifier or not commands:
        return None
    return {"id": identifier, "commands": commands}


def _looks_like_command_call(event: Any) -> bool:
    """Whether a typed tool call is intended to execute a shell command.

    CATX sessions routinely contain non-command calls such as ``read_file``
    whose payload has no executable command.  Those calls are irrelevant to
    repository binding and must not be treated as malformed command evidence.
    A command-shaped payload or a well-known command tool name, however, is
    checked fail-closed when its id/command structure is incomplete.
    """

    if not isinstance(event, Mapping) or _event_kind(event) not in _TOOL_CALL_TYPES:
        return False
    name = event.get("name") or event.get("tool")
    if isinstance(name, str) and name.strip().casefold() in _COMMAND_TOOL_NAMES:
        return True
    return bool(_command_values(event))


def _structured_result(event: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(event, Mapping) or _event_kind(event) not in _TOOL_RESULT_TYPES:
        return None
    identifier = _call_id(event, result=True)
    values = _result_values(event)
    if not identifier or not values:
        return None
    is_error = event.get("is_error")
    ok = event.get("ok")
    success = event.get("success")
    exit_code = event.get("exit_code", event.get("returncode"))
    status = event.get("status") or event.get("state")
    if is_error is True or ok is False or success is False:
        return {"id": identifier, "values": values, "success": False}
    if isinstance(exit_code, bool) or (exit_code is not None and str(exit_code) != "0"):
        return {"id": identifier, "values": values, "success": False}
    if status is not None and str(status).casefold() not in _SUCCESS_STATUSES:
        return {"id": identifier, "values": values, "success": False}
    return {"id": identifier, "values": values, "success": True}


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
        # Never search ``str(event)`` here.  A model message or a tool result
        # can contain a quoted command/path without that command having run.
        # Only a typed tool-use with a structured command and a paired typed
        # tool-result is admissible binding evidence.
        calls: dict[str, dict[str, Any]] = {}
        results: dict[str, list[dict[str, Any]]] = {}
        malformed_calls = 0
        malformed_results = 0
        for index, event in enumerate(events):
            call = _structured_call(event)
            if call is not None:
                call = dict(call)
                call["event_index"] = index
                if call["id"] in calls:
                    # Duplicate call ids make the association ambiguous even
                    # if one of the duplicate events happens to look valid.
                    malformed_calls += 1
                else:
                    calls[str(call["id"])] = call
                continue
            if isinstance(event, Mapping) and _event_kind(event) in _TOOL_CALL_TYPES:
                # Ignore ordinary non-command tools (read_file, list_dir,
                # browser actions, ...).  Only a command-shaped event is a
                # binding assertion that must satisfy the strict schema.
                if _looks_like_command_call(event):
                    malformed_calls += 1
                continue
            result = _structured_result(event)
            if result is not None:
                result = dict(result)
                result["event_index"] = index
                results.setdefault(str(result["id"]), []).append(result)
            elif isinstance(event, Mapping) and _event_kind(event) in _TOOL_RESULT_TYPES:
                # A non-command tool result is not binding evidence.  A
                # malformed result for a known command call is still a hard
                # failure; unrelated result shapes may safely be ignored.
                result_id = _call_id(event, result=True)
                if result_id and result_id in calls:
                    malformed_results += 1

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
                "mount_observed": False,
                "revision_observed": False,
            })
        if not checks:
            # A single-repository binding still has a useful mount path in the
            # profile; use it as the minimum event-log assertion.  The
            # revision is still checked against a full SHA in the result.
            resources = tuple(getattr(client.profile, "repository_resources", ()) or ())
            match = next((item for item in resources if getattr(item, "url", None) == binding.repository_ref), None)
            mount = getattr(match, "mount_path", "") if match is not None else ""
            checks = [{
                "mount_path": mount,
                "revision": binding.head_commit,
                "mount_observed": False,
                "revision_observed": False,
            }]
        for check in checks:
            mount = os.path.normpath(str(check["mount_path"])) if check.get("mount_path") else ""
            revision = str(check.get("revision") or "").casefold()
            matching_calls = []
            for call in calls.values():
                command_mounts = tuple(
                    mount_value
                    for command in call.get("commands", ())
                    if (mount_value := _command_mount(str(command))) is not None
                )
                if mount and mount in command_mounts:
                    matching_calls.append(call)
            selected = None
            selected_result = None
            for call in matching_calls:
                paired = [
                    result
                    for result in results.get(str(call["id"]), ())
                    if int(result.get("event_index", -1)) > int(call.get("event_index", -1))
                ]
                if not paired:
                    continue
                # A call id should have one authoritative result. Multiple
                # results are ambiguous and therefore not binding evidence.
                if len(paired) != 1:
                    continue
                result = paired[0]
                observed_shas = {
                    match.group(1).casefold()
                    for value in result.get("values", ())
                    for match in _FULL_SHA.finditer(str(value))
                }
                if not result.get("success") or not observed_shas:
                    continue
                if revision and revision not in observed_shas:
                    continue
                selected, selected_result = call, result
                break
            if selected is not None and selected_result is not None:
                observed_shas = sorted(
                    {
                        match.group(1).casefold()
                        for value in selected_result.get("values", ())
                        for match in _FULL_SHA.finditer(str(value))
                    }
                )
                check.update(
                    {
                        "mount_observed": True,
                        "revision_observed": (not revision) or revision in observed_shas,
                        "call_id": selected["id"],
                        "call_event_index": selected.get("event_index"),
                        "result_event_index": selected_result.get("event_index"),
                        "observed_commits": observed_shas,
                        "result_success": bool(selected_result.get("success")),
                    }
                )
            else:
                check.update({"result_success": False, "observed_commits": []})
        verified = bool(checks) and malformed_calls == 0 and malformed_results == 0 and all(
            item["mount_observed"] and item["revision_observed"] for item in checks
        )
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
                "tool_call_count": len(calls),
                "tool_result_count": sum(len(items) for items in results.values()),
                "malformed_tool_call_count": malformed_calls,
                "malformed_tool_result_count": malformed_results,
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
