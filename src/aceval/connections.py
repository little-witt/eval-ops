"""Company Agent API profiles and fail-closed session-log import.

The connector deliberately has a narrow boundary:

* profiles contain an environment-variable *name*, never a credential value;
* HTTP is implemented with the standard library and can be replaced in tests;
* remote payloads are normalized into the project-owned observation contracts;
* malformed or ambiguously mapped telemetry is rejected instead of guessed.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Dict,
    FrozenSet,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
    runtime_checkable,
)

from .contracts import RunObservation, RuntimeResult, TraceEvent, canonical_trace_kind


PROFILE_API_VERSION = "aceval.company-profile/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PROFILE_BYTES = 1024 * 1024


class CompanyApiProfileError(ValueError):
    """Raised when a company API profile is malformed."""


class JsonPathError(ValueError):
    """Raised for an unsupported JSON path or a missing path component."""


class CompanyApiRequestError(RuntimeError):
    """Raised when a company API request cannot be safely completed."""


class SessionLogImportError(RuntimeError):
    """Raised when a session log cannot be normalized without guessing."""


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number is not allowed: %s" % value)


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: %s" % key)
        result[key] = value
    return result


def _decode_json_bytes(value: bytes, label: str, max_bytes: int) -> Any:
    if not isinstance(value, bytes):
        raise ValueError("%s must be UTF-8 JSON bytes" % label)
    if len(value) > max_bytes:
        raise ValueError("%s exceeds byte limit" % label)
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("%s is not valid UTF-8" % label) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("%s is not strict JSON" % label) from exc


def _mapping(value: Any, label: str, error_type: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise error_type("%s must be an object" % label)
    if any(not isinstance(key, str) for key in value):
        raise error_type("%s keys must be strings" % label)
    return value


def _reject_unknown(
    value: Mapping[str, Any], allowed: FrozenSet[str], label: str
) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise CompanyApiProfileError(
            "%s contains unsupported fields: %s" % (label, ", ".join(unknown))
        )


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CompanyApiProfileError("%s must be a non-empty trimmed string" % label)
    if any(ord(character) < 32 for character in value):
        raise CompanyApiProfileError("%s must not contain control characters" % label)
    return value


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_DOT_PATH_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


JsonPathSegment = Tuple[str, Union[str, int]]


def _parse_json_path(path: Any) -> Tuple[JsonPathSegment, ...]:
    if not isinstance(path, str) or not path.startswith("$"):
        raise JsonPathError("JSON path must start with $")
    segments = []
    index = 1
    while index < len(path):
        if path[index] == ".":
            start = index + 1
            end = start
            while end < len(path) and path[end] not in ".[":
                end += 1
            key = path[start:end]
            if not _DOT_PATH_KEY.fullmatch(key):
                raise JsonPathError("invalid dotted JSON path component in %s" % path)
            segments.append(("key", key))
            index = end
            continue
        if path[index] != "[":
            raise JsonPathError("unsupported JSON path syntax in %s" % path)
        content_start = index + 1
        if content_start >= len(path):
            raise JsonPathError("unterminated JSON path bracket in %s" % path)
        if path[content_start] in ('"', "'"):
            if path[content_start] == "'":
                raise JsonPathError(
                    "quoted JSON path keys must use JSON double quotes in %s" % path
                )
            try:
                key, consumed = json.JSONDecoder().raw_decode(path[content_start:])
            except json.JSONDecodeError as exc:
                raise JsonPathError("invalid quoted JSON path key in %s" % path) from exc
            if not isinstance(key, str):
                raise JsonPathError("JSON path bracket key must be a string")
            close = content_start + consumed
            if close >= len(path) or path[close] != "]":
                raise JsonPathError("unterminated JSON path bracket in %s" % path)
            segments.append(("key", key))
            index = close + 1
            continue
        close = path.find("]", content_start)
        if close < 0:
            raise JsonPathError("unterminated JSON path bracket in %s" % path)
        raw_index = path[content_start:close]
        if not raw_index.isdigit():
            raise JsonPathError("JSON path array indexes must be non-negative integers")
        segments.append(("index", int(raw_index)))
        index = close + 1
    return tuple(segments)


def extract_json_path(document: Any, path: str) -> Any:
    """Extract a value using a deterministic JSONPath subset.

    Supported forms are ``$``, dotted object keys, JSON-double-quoted bracket
    keys, and non-negative array indexes. Wildcards, filters, recursive
    descent, and expressions are intentionally rejected.
    """

    current = document
    for kind, component in _parse_json_path(path):
        if kind == "key":
            if not isinstance(current, Mapping) or component not in current:
                raise JsonPathError("JSON path not found: %s" % path)
            current = current[component]
        else:
            if (
                not isinstance(current, (list, tuple))
                or isinstance(current, (str, bytes))
                or int(component) >= len(current)
            ):
                raise JsonPathError("JSON path not found: %s" % path)
            current = current[int(component)]
    return current


def _optional_json_path(document: Any, path: Optional[str]) -> Tuple[bool, Any]:
    if path is None:
        return False, None
    try:
        return True, extract_json_path(document, path)
    except JsonPathError as exc:
        if "not found" not in str(exc):
            raise
        return False, None


def _validate_json_path(value: Any, label: str, required: bool = False) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise CompanyApiProfileError("%s must be a JSON path string" % label)
    try:
        _parse_json_path(value)
    except JsonPathError as exc:
        raise CompanyApiProfileError("invalid %s: %s" % (label, exc)) from exc
    return value


def _validate_endpoint_path(value: Any, label: str, template: bool = False) -> str:
    path = _required_string(value, label)
    parsed = urllib.parse.urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise CompanyApiProfileError(
            "%s must be an absolute API path without host, query, or fragment" % label
        )
    if template:
        if path.count("{session_id}") != 1:
            raise CompanyApiProfileError(
                "%s must contain {session_id} exactly once" % label
            )
        remainder = path.replace("{session_id}", "")
        if "{" in remainder or "}" in remainder:
            raise CompanyApiProfileError("%s contains an unsupported placeholder" % label)
    elif "{" in path or "}" in path:
        raise CompanyApiProfileError("%s must not contain placeholders" % label)
    return path


@dataclass(frozen=True)
class EnvironmentAuth:
    """Authentication configuration that stores only an environment name."""

    type: str
    env: str
    header: Optional[str] = None

    def __post_init__(self) -> None:
        if self.type not in ("bearer_env", "header_env"):
            raise CompanyApiProfileError(
                "auth.type must be bearer_env or header_env"
            )
        if not isinstance(self.env, str) or not _ENV_NAME.fullmatch(self.env):
            raise CompanyApiProfileError("auth.env must be a valid environment name")
        if self.type == "bearer_env":
            if self.header is not None:
                raise CompanyApiProfileError(
                    "auth.header is only allowed for header_env authentication"
                )
        elif not isinstance(self.header, str) or not _HEADER_NAME.fullmatch(self.header):
            raise CompanyApiProfileError(
                "auth.header must be a valid HTTP header name for header_env"
            )

    @classmethod
    def from_mapping(cls, value: Any) -> "EnvironmentAuth":
        mapping = _mapping(value, "auth", CompanyApiProfileError)
        _reject_unknown(mapping, frozenset(("type", "env", "header")), "auth")
        return cls(
            type=mapping.get("type"),
            env=mapping.get("env"),
            header=mapping.get("header"),
        )

    def request_headers(self, environment: Mapping[str, str]) -> Mapping[str, str]:
        if not isinstance(environment, Mapping):
            raise CompanyApiRequestError("authentication environment is invalid")
        secret = environment.get(self.env)
        if not isinstance(secret, str) or not secret.strip():
            raise CompanyApiRequestError(
                "authentication environment variable is not configured: %s" % self.env
            )
        if "\r" in secret or "\n" in secret or "\x00" in secret:
            raise CompanyApiRequestError("authentication environment value is invalid")
        if self.type == "bearer_env":
            return {"Authorization": "Bearer " + secret}
        return {str(self.header): secret}


@dataclass(frozen=True)
class ExecuteEndpointMapping:
    path: str
    method: str = "POST"
    session_id_path: str = "$.session_id"

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validate_endpoint_path(self.path, "execute.path"))
        if self.method != "POST":
            raise CompanyApiProfileError("execute.method must be POST")
        object.__setattr__(
            self,
            "session_id_path",
            _validate_json_path(
                self.session_id_path, "execute.session_id_path", required=True
            ),
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "ExecuteEndpointMapping":
        mapping = _mapping(value, "execute", CompanyApiProfileError)
        _reject_unknown(
            mapping,
            frozenset(("path", "method", "session_id_path")),
            "execute",
        )
        return cls(
            path=mapping.get("path"),
            method=mapping.get("method", "POST"),
            session_id_path=mapping.get("session_id_path", "$.session_id"),
        )


@dataclass(frozen=True)
class SessionLogEndpointMapping:
    path_template: str
    method: str = "GET"
    output_path: str = "$.output"
    trace_path: Optional[str] = "$.events"
    usage_path: Optional[str] = "$.usage"
    error_path: Optional[str] = None
    metadata_path: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "path_template",
            _validate_endpoint_path(
                self.path_template, "session_log.path_template", template=True
            ),
        )
        if self.method != "GET":
            raise CompanyApiProfileError("session_log.method must be GET")
        for name, required in (
            ("output_path", True),
            ("trace_path", False),
            ("usage_path", False),
            ("error_path", False),
            ("metadata_path", False),
        ):
            object.__setattr__(
                self,
                name,
                _validate_json_path(
                    getattr(self, name), "session_log.%s" % name, required=required
                ),
            )

    @classmethod
    def from_mapping(cls, value: Any) -> "SessionLogEndpointMapping":
        mapping = _mapping(value, "session_log", CompanyApiProfileError)
        allowed = frozenset(
            (
                "path_template",
                "method",
                "output_path",
                "trace_path",
                "usage_path",
                "error_path",
                "metadata_path",
            )
        )
        _reject_unknown(mapping, allowed, "session_log")
        return cls(
            path_template=mapping.get("path_template"),
            method=mapping.get("method", "GET"),
            output_path=mapping.get("output_path", "$.output"),
            trace_path=mapping.get("trace_path", "$.events"),
            usage_path=mapping.get("usage_path", "$.usage"),
            error_path=mapping.get("error_path"),
            metadata_path=mapping.get("metadata_path"),
        )


def _base_url(value: Any) -> str:
    url = _required_string(value, "base_url")
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CompanyApiProfileError(
            "base_url must be an http(s) URL without credentials, query, or fragment"
        )
    return url.rstrip("/")


def _positive_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise CompanyApiProfileError("%s must be a positive finite number" % label)
    return float(value)


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CompanyApiProfileError("%s must be a positive integer" % label)
    return value


@dataclass(frozen=True)
class CompanyApiProfile:
    api_version: str
    name: str
    base_url: str
    auth: EnvironmentAuth
    session_log: SessionLogEndpointMapping
    execute: Optional[ExecuteEndpointMapping] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if self.api_version != PROFILE_API_VERSION:
            raise CompanyApiProfileError(
                "unsupported company profile api_version: %s" % self.api_version
            )
        object.__setattr__(self, "name", _required_string(self.name, "name"))
        object.__setattr__(self, "base_url", _base_url(self.base_url))
        if not isinstance(self.auth, EnvironmentAuth):
            raise CompanyApiProfileError("auth must be an EnvironmentAuth")
        if not isinstance(self.session_log, SessionLogEndpointMapping):
            raise CompanyApiProfileError(
                "session_log must be a SessionLogEndpointMapping"
            )
        if self.execute is not None and not isinstance(
            self.execute, ExecuteEndpointMapping
        ):
            raise CompanyApiProfileError("execute must be an ExecuteEndpointMapping")
        object.__setattr__(
            self,
            "timeout_seconds",
            _positive_number(self.timeout_seconds, "timeout_seconds"),
        )
        object.__setattr__(
            self,
            "max_response_bytes",
            _positive_integer(self.max_response_bytes, "max_response_bytes"),
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "CompanyApiProfile":
        mapping = _mapping(value, "company API profile", CompanyApiProfileError)
        allowed = frozenset(
            (
                "api_version",
                "name",
                "base_url",
                "auth",
                "execute",
                "session_log",
                "timeout_seconds",
                "max_response_bytes",
            )
        )
        _reject_unknown(mapping, allowed, "company API profile")
        execute = mapping.get("execute")
        return cls(
            api_version=mapping.get("api_version"),
            name=mapping.get("name"),
            base_url=mapping.get("base_url"),
            auth=EnvironmentAuth.from_mapping(mapping.get("auth")),
            execute=(
                ExecuteEndpointMapping.from_mapping(execute)
                if execute is not None
                else None
            ),
            session_log=SessionLogEndpointMapping.from_mapping(
                mapping.get("session_log")
            ),
            timeout_seconds=mapping.get(
                "timeout_seconds", DEFAULT_TIMEOUT_SECONDS
            ),
            max_response_bytes=mapping.get(
                "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES
            ),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "CompanyApiProfile":
        profile_path = Path(path)
        try:
            raw = profile_path.read_bytes()
        except OSError as exc:
            raise CompanyApiProfileError("cannot read company API profile") from exc
        try:
            payload = _decode_json_bytes(raw, "company API profile", MAX_PROFILE_BYTES)
        except ValueError as exc:
            raise CompanyApiProfileError(str(exc)) from exc
        return cls.from_mapping(payload)


def load_company_api_profile(path: Union[str, Path]) -> CompanyApiProfile:
    return CompanyApiProfile.load(path)


_OBSERVATION_FIELDS = frozenset(("output", "trace", "usage", "error", "metadata"))


@dataclass(frozen=True)
class ObservationCompleteness:
    """Which configured observation channels were actually measured."""

    expected: FrozenSet[str] = field(default_factory=frozenset)
    observed: FrozenSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        expected = frozenset(self.expected)
        observed = frozenset(self.observed)
        invalid = (expected | observed) - _OBSERVATION_FIELDS
        if invalid:
            raise ValueError(
                "unsupported observation completeness fields: %s"
                % ", ".join(sorted(invalid))
            )
        object.__setattr__(self, "expected", expected)
        object.__setattr__(self, "observed", observed)

    @property
    def complete(self) -> bool:
        return self.expected.issubset(self.observed)

    @property
    def missing(self) -> Tuple[str, ...]:
        return tuple(sorted(self.expected - self.observed))

    @property
    def output(self) -> bool:
        return "output" in self.observed

    @property
    def trace(self) -> bool:
        return "trace" in self.observed

    @property
    def usage(self) -> bool:
        return "usage" in self.observed

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "output": self.output,
            "trace": self.trace,
            "usage": self.usage,
            "error": "error" in self.observed,
            "metadata": "metadata" in self.observed,
            "expected": sorted(self.expected),
            "observed": sorted(self.observed),
            "missing": list(self.missing),
            "complete": self.complete,
        }


@dataclass(frozen=True)
class ImportedRunBundle:
    session_id: str
    profile_name: str
    observation: RunObservation
    completeness: ObservationCompleteness
    source: str = "company_session_log"

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(self.profile_name, str) or not self.profile_name:
            raise ValueError("profile_name must be a non-empty string")
        if not isinstance(self.observation, RunObservation):
            raise TypeError("observation must be a RunObservation")
        if not isinstance(self.completeness, ObservationCompleteness):
            raise TypeError("completeness must be an ObservationCompleteness")

    @property
    def output(self) -> Any:
        return self.observation.output

    @property
    def trace(self) -> Tuple[TraceEvent, ...]:
        return self.observation.trace

    @property
    def usage(self) -> Mapping[str, Any]:
        return self.observation.usage

    def to_runtime_result(self) -> RuntimeResult:
        return RuntimeResult(
            final_output=self.observation.output,
            trace=self.observation.trace,
            error=self.observation.error,
            usage=self.observation.usage,
            artifacts=self.observation.artifacts,
            metadata=self.observation.metadata,
        )


def _session_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(ord(character) < 32 for character in value)
    ):
        raise SessionLogImportError("session_id must be a non-empty safe string")
    return value


def _trace_event(value: Any, fallback_seq: int) -> TraceEvent:
    if not isinstance(value, Mapping):
        raise SessionLogImportError("trace events must be objects")
    raw_kind = value.get("kind")
    raw_type = value.get("type")
    if raw_kind is not None and raw_type is not None:
        if canonical_trace_kind({"kind": raw_kind}) != canonical_trace_kind(
            {"kind": raw_type}
        ):
            raise SessionLogImportError("trace event kind/type aliases conflict")
    kind_value = raw_kind if raw_kind is not None else raw_type
    if not isinstance(kind_value, str) or not kind_value:
        raise SessionLogImportError("trace event kind must be a non-empty string")
    kind = canonical_trace_kind({"kind": kind_value})

    seq = value.get("seq", fallback_seq)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise SessionLogImportError("trace event seq must be a non-negative integer")
    timestamp = value.get("timestamp")
    if timestamp is not None and not isinstance(timestamp, str):
        raise SessionLogImportError("trace event timestamp must be a string")
    name = value.get("name", "")
    if not isinstance(name, str):
        raise SessionLogImportError("trace event name must be a string")
    tool = value.get("tool")
    if tool is not None and (not isinstance(tool, str) or not tool):
        raise SessionLogImportError("trace event tool must be a non-empty string")
    if kind == "tool_call" and tool is None and name:
        tool = name
    duration = value.get("duration_ms")
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration < 0
    ):
        raise SessionLogImportError(
            "trace event duration_ms must be a non-negative finite number"
        )
    error = value.get("error")
    if error is not None and not isinstance(error, str):
        raise SessionLogImportError("trace event error must be a string")

    known = frozenset(
        (
            "kind",
            "type",
            "seq",
            "timestamp",
            "name",
            "tool",
            "duration_ms",
            "error",
            "payload",
        )
    )
    payload = value.get("payload", {})
    if not isinstance(payload, Mapping):
        raise SessionLogImportError("trace event payload must be an object")
    if any(not isinstance(key, str) for key in payload):
        raise SessionLogImportError("trace event payload keys must be strings")
    extras = {key: item for key, item in value.items() if key not in known}
    merged_payload = dict(payload)
    for key, item in extras.items():
        if key in merged_payload and merged_payload[key] != item:
            raise SessionLogImportError(
                "trace event payload conflicts with top-level field: %s" % key
            )
        merged_payload[key] = item
    return TraceEvent(
        kind=kind,
        seq=seq,
        timestamp=timestamp,
        name=name,
        payload=merged_payload,
        tool=tool,
        duration_ms=float(duration) if duration is not None else None,
        error=error,
    )


def _trace(value: Any) -> Tuple[TraceEvent, ...]:
    if not isinstance(value, list):
        raise SessionLogImportError("mapped trace must be an array")
    return tuple(_trace_event(event, index) for index, event in enumerate(value))


def _usage(value: Any) -> Mapping[str, Any]:
    usage = _mapping(value, "mapped usage", SessionLogImportError)
    token_fields = frozenset(
        (
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
        )
    )
    result = {}
    for key, item in usage.items():
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or item < 0
        ):
            raise SessionLogImportError(
                "usage values must be non-negative finite numbers"
            )
        if key in token_fields and not isinstance(item, int):
            raise SessionLogImportError("token usage values must be integers")
        result[key] = item
    return result


def import_session_log(
    profile: CompanyApiProfile, session_id: str, payload: Any
) -> ImportedRunBundle:
    """Normalize a decoded remote session payload into project contracts."""

    if not isinstance(profile, CompanyApiProfile):
        raise TypeError("profile must be a CompanyApiProfile")
    session = _session_id(session_id)
    document = _mapping(payload, "session log", SessionLogImportError)
    endpoint = profile.session_log

    expected = {"output"}
    observed = set()
    try:
        output = extract_json_path(document, endpoint.output_path)
    except JsonPathError as exc:
        raise SessionLogImportError("mapped output is missing") from exc
    observed.add("output")

    trace: Tuple[TraceEvent, ...] = ()
    if endpoint.trace_path is not None:
        expected.add("trace")
        try:
            trace = _trace(extract_json_path(document, endpoint.trace_path))
        except JsonPathError as exc:
            raise SessionLogImportError("mapped trace is missing") from exc
        observed.add("trace")

    usage: Mapping[str, Any] = {}
    if endpoint.usage_path is not None:
        expected.add("usage")
        try:
            usage = _usage(extract_json_path(document, endpoint.usage_path))
        except JsonPathError as exc:
            raise SessionLogImportError("mapped usage is missing") from exc
        observed.add("usage")

    error_present, error = _optional_json_path(document, endpoint.error_path)
    if error_present:
        if error is not None and not isinstance(error, str):
            raise SessionLogImportError("mapped error must be a string or null")
        observed.add("error")

    metadata_present, source_metadata = _optional_json_path(
        document, endpoint.metadata_path
    )
    if metadata_present:
        source_metadata = _mapping(
            source_metadata, "mapped metadata", SessionLogImportError
        )
        observed.add("metadata")
    else:
        source_metadata = {}

    completeness = ObservationCompleteness(
        expected=frozenset(expected), observed=frozenset(observed)
    )
    observation_metadata = {
        "source": "company_session_log",
        "profile_name": profile.name,
        "session_id": session,
        "completeness": {
            "output": completeness.output,
            "trace": completeness.trace,
            "usage": completeness.usage,
        },
        "observation_completeness": completeness.as_dict(),
    }
    if source_metadata:
        observation_metadata["source_metadata"] = dict(source_metadata)
    observation = RunObservation(
        output=output,
        trace=trace,
        error=error if error_present else None,
        usage=usage,
        metadata=observation_metadata,
    )
    return ImportedRunBundle(
        session_id=session,
        profile_name=profile.name,
        observation=observation,
        completeness=completeness,
    )


def load_session_log(
    profile: CompanyApiProfile, session_id: str, path: Union[str, Path]
) -> ImportedRunBundle:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise SessionLogImportError("cannot read session log") from exc
    try:
        payload = _decode_json_bytes(
            raw, "session log", profile.max_response_bytes
        )
    except ValueError as exc:
        raise SessionLogImportError(str(exc)) from exc
    return import_session_log(profile, session_id, payload)


@dataclass(frozen=True)
class HTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or not 100 <= self.status_code <= 599
        ):
            raise ValueError("HTTP response status_code is invalid")
        if not isinstance(self.body, bytes):
            raise TypeError("HTTP response body must be bytes")
        if not isinstance(self.headers, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.headers.items()
        ):
            raise TypeError("HTTP response headers must map strings to strings")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@runtime_checkable
class HTTPTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> HTTPResponse:
        ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


def _read_limited(response: Any, max_bytes: int) -> bytes:
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise CompanyApiRequestError("company API response exceeds byte limit")
    return body


class UrllibHTTPTransport:
    """Small stdlib transport. Redirects are rejected to avoid auth leakage."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> HTTPResponse:
        request = urllib.request.Request(
            url=url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                return HTTPResponse(
                    status_code=int(response.status),
                    body=_read_limited(response, max_response_bytes),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            with exc:
                return HTTPResponse(
                    status_code=int(exc.code),
                    body=_read_limited(exc, max_response_bytes),
                    headers=dict(exc.headers.items()) if exc.headers else {},
                )
        except urllib.error.URLError as exc:
            raise CompanyApiRequestError("company API transport failed") from exc


@runtime_checkable
class SessionLogProvider(Protocol):
    def fetch_session(self, session_id: str) -> ImportedRunBundle:
        ...


class HTTPSessionLogProvider:
    """Fetches and imports sessions according to a validated profile."""

    def __init__(
        self,
        profile: CompanyApiProfile,
        *,
        transport: Optional[HTTPTransport] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        if not isinstance(profile, CompanyApiProfile):
            raise TypeError("profile must be a CompanyApiProfile")
        if environment is not None and not isinstance(environment, Mapping):
            raise TypeError("environment must be a mapping")
        self.profile = profile
        self._transport = transport if transport is not None else UrllibHTTPTransport()
        self._environment = environment if environment is not None else os.environ

    @classmethod
    def from_profile_json(
        cls,
        path: Union[str, Path],
        *,
        transport: Optional[HTTPTransport] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> "HTTPSessionLogProvider":
        return cls(
            CompanyApiProfile.load(path),
            transport=transport,
            environment=environment,
        )

    def _url(self, path: str) -> str:
        return self.profile.base_url + path

    def _request_json(
        self, method: str, path: str, body: Optional[bytes]
    ) -> Mapping[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "aceval-company-connector/1",
        }
        headers.update(self.profile.auth.request_headers(self._environment))
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            response = self._transport.request(
                method=method,
                url=self._url(path),
                headers=headers,
                body=body,
                timeout_seconds=self.profile.timeout_seconds,
                max_response_bytes=self.profile.max_response_bytes,
            )
        except CompanyApiRequestError:
            raise
        except Exception as exc:
            raise CompanyApiRequestError("company API transport failed") from exc
        if not isinstance(response, HTTPResponse):
            raise CompanyApiRequestError("company API transport returned an invalid response")
        if not 200 <= response.status_code < 300:
            raise CompanyApiRequestError(
                "company API returned HTTP status %d" % response.status_code
            )
        try:
            payload = _decode_json_bytes(
                response.body,
                "company API response",
                self.profile.max_response_bytes,
            )
        except ValueError as exc:
            raise CompanyApiRequestError(str(exc)) from exc
        return _mapping(payload, "company API response", CompanyApiRequestError)

    def execute(self, payload: Mapping[str, Any]) -> str:
        endpoint = self.profile.execute
        if endpoint is None:
            raise CompanyApiRequestError(
                "company API profile does not configure an execute endpoint"
            )
        request_payload = _mapping(
            payload, "execute payload", CompanyApiRequestError
        )
        try:
            body = json.dumps(
                request_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise CompanyApiRequestError("execute payload is not strict JSON") from exc
        response = self._request_json(endpoint.method, endpoint.path, body)
        try:
            session_id = extract_json_path(response, endpoint.session_id_path)
        except JsonPathError as exc:
            raise CompanyApiRequestError("execute response is missing session_id") from exc
        try:
            return _session_id(session_id)
        except SessionLogImportError as exc:
            raise CompanyApiRequestError("execute response has an invalid session_id") from exc

    def fetch_session(self, session_id: str) -> ImportedRunBundle:
        session = _session_id(session_id)
        encoded = urllib.parse.quote(session, safe="")
        endpoint = self.profile.session_log
        path = endpoint.path_template.replace("{session_id}", encoded)
        payload = self._request_json(endpoint.method, path, None)
        return import_session_log(self.profile, session, payload)


__all__ = [
    "PROFILE_API_VERSION",
    "CompanyApiProfileError",
    "JsonPathError",
    "CompanyApiRequestError",
    "SessionLogImportError",
    "EnvironmentAuth",
    "ExecuteEndpointMapping",
    "SessionLogEndpointMapping",
    "CompanyApiProfile",
    "ObservationCompleteness",
    "ImportedRunBundle",
    "HTTPResponse",
    "HTTPTransport",
    "UrllibHTTPTransport",
    "SessionLogProvider",
    "HTTPSessionLogProvider",
    "extract_json_path",
    "load_company_api_profile",
    "import_session_log",
    "load_session_log",
]
