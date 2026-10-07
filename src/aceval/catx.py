"""CATX online Agent session connector and Supabase SSE listener.

The CATX Session API is a two-step protocol: create a session, then append a
``user.message`` event.  This module loads credentials from an optional local,
permission-restricted JSON file with environment-variable overrides, normalizes
complete CATX event logs into :class:`ImportedRunBundle`, and can wait for
terminal state through the optional Supabase SSE proxy.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Protocol, Sequence, Tuple, Union

from .connections import (
    CompanyApiRequestError,
    HTTPResponse,
    HTTPTransport,
    ImportedRunBundle,
    ObservationCompleteness,
    UrllibHTTPTransport,
)
from .contracts import RunObservation, TraceEvent
from .catx_bindings import (
    CatxBindingEvidence,
    CatxExecutionBinding,
    CatxSessionBindingAdapter,
)


CATX_PROFILE_API_VERSION = "aceval.catx-profile/v1"
DEFAULT_CATX_BASE_URL = "https://api.catx.sankuai.com/api/v1"
# CATX's Events endpoint accepts up to 1000 events per request.  A 200-event
# default was too small for normal repository reviews: once a response landed
# exactly on that boundary, the legacy API supplied no pagination metadata and
# we had to fail closed with ``pagination_not_proven_complete``.  Request the
# documented maximum by default so ordinary completed sessions arrive in one
# complete snapshot; callers can still lower this explicitly when needed.
DEFAULT_EVENTS_LIMIT = 1000
MAX_PROFILE_BYTES = 1024 * 1024
MAX_CREDENTIALS_BYTES = 64 * 1024
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CatxProfileError(ValueError):
    """Raised when a CATX profile is malformed."""


class CatxStreamError(CompanyApiRequestError):
    """Raised when the configured SSE stream is unavailable or malformed."""


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: %s" % key)
        result[key] = value
    return result


def _strict_json(raw: bytes, label: str, max_bytes: int) -> Any:
    if len(raw) > max_bytes:
        raise ValueError("%s exceeds byte limit" % label)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("%s is not valid UTF-8" % label) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number: %s" % value)
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("%s is not strict JSON" % label) from exc


def _mapping(value: Any, label: str, error_type: Any = CatxProfileError) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise error_type("%s must be an object with string keys" % label)
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: Iterable[str], label: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise CatxProfileError(
            "%s contains unsupported fields: %s" % (label, ", ".join(unknown))
        )


def _trimmed(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise CatxProfileError("%s must be a non-empty safe string" % label)
    return value


def _env_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
        raise CatxProfileError("%s must be a valid environment variable name" % label)
    return value


def _base_url(value: Any, label: str) -> str:
    url = _trimmed(value, label)
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CatxProfileError(
            "%s must be an http(s) URL without credentials, query, or fragment" % label
        )
    return url.rstrip("/")


def _repository_url(value: Any, label: str) -> str:
    url = _trimmed(value, label)
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in ("ssh", "https", "http")
        or not parsed.netloc
        or parsed.password is not None
        or not parsed.path
        or parsed.path == "/"
        or parsed.query
        or parsed.fragment
    ):
        raise CatxProfileError(
            "%s must be an ssh/http(s) repository URL without password, query, or fragment"
            % label
        )
    return url


def _mount_path(value: Any, label: str) -> str:
    text = _trimmed(value, label)
    if "\\" in text or text.startswith("//"):
        raise CatxProfileError("%s must be an absolute POSIX path" % label)
    path = PurePosixPath(text)
    if not path.is_absolute() or any(part in (".", "..") for part in path.parts):
        raise CatxProfileError("%s must be an absolute normalized POSIX path" % label)
    return path.as_posix()


def _positive_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise CatxProfileError("%s must be a positive finite number" % label)
    return float(value)


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CatxProfileError("%s must be a positive integer" % label)
    return value


def _environment_value(environment: Mapping[str, str], name: str, label: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\r" in value
        or "\n" in value
        or "\x00" in value
    ):
        raise CompanyApiRequestError(
            "%s environment variable is not safely configured: %s" % (label, name)
        )
    return value


def _load_credentials_file(
    path: Union[str, Path], allowed_names: Iterable[str]
) -> Mapping[str, str]:
    credentials_path = Path(path)
    try:
        metadata = credentials_path.lstat()
    except OSError as exc:
        raise CatxProfileError("cannot read CATX credentials file") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CatxProfileError("CATX credentials file must be a regular non-symlink file")
    if os.name == "posix" and metadata.st_mode & 0o077:
        raise CatxProfileError(
            "CATX credentials file permissions are too open; expected mode 0600"
        )
    try:
        raw = credentials_path.read_bytes()
        value = _strict_json(raw, "CATX credentials file", MAX_CREDENTIALS_BYTES)
    except OSError as exc:
        raise CatxProfileError("cannot read CATX credentials file") from exc
    except ValueError as exc:
        raise CatxProfileError(str(exc)) from exc
    mapping = _mapping(value, "CATX credentials file")
    allowed = set(allowed_names)
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise CatxProfileError(
            "CATX credentials file contains unreferenced keys: %s"
            % ", ".join(unknown)
        )
    result = {}
    for name, credential in mapping.items():
        _env_name(name, "CATX credentials key")
        if (
            not isinstance(credential, str)
            or not credential.strip()
            or "\r" in credential
            or "\n" in credential
            or "\x00" in credential
        ):
            raise CatxProfileError(
                "CATX credentials file value is unsafe for key: %s" % name
            )
        result[name] = credential
    return result


_URL_USERINFO = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)


def _redact_sensitive(value: Any, sensitive_values: Sequence[str]) -> Any:
    """Recursively redact configured credentials and URL userinfo from logs."""

    if isinstance(value, str):
        redacted = _URL_USERINFO.sub(r"\1***@", value)
        for secret in sensitive_values:
            redacted = redacted.replace(secret, "***")
            encoded = urllib.parse.quote(secret, safe="")
            if encoded != secret:
                redacted = redacted.replace(encoded, "***")
        return redacted
    if isinstance(value, Mapping):
        return {key: _redact_sensitive(item, sensitive_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive(item, sensitive_values) for item in value)
    return value


def _safe_session_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(ord(character) < 32 for character in value)
    ):
        raise CompanyApiRequestError("CATX session id must be a non-empty safe string")
    return value


@dataclass(frozen=True)
class SupabaseSSEProfile:
    base_url: str
    api_key_env: str
    bearer_env: Optional[str] = None
    path: str = "/functions/v1/nocode-catx-agent"
    action: str = "getMessage"
    max_stream_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _base_url(self.base_url, "stream.base_url"))
        object.__setattr__(self, "api_key_env", _env_name(self.api_key_env, "stream.api_key_env"))
        if self.bearer_env is not None:
            object.__setattr__(self, "bearer_env", _env_name(self.bearer_env, "stream.bearer_env"))
        path = _trimmed(self.path, "stream.path")
        parsed = urllib.parse.urlsplit(path)
        if not path.startswith("/") or path.startswith("//") or parsed.query or parsed.fragment:
            raise CatxProfileError("stream.path must be an absolute path without query or fragment")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "action", _trimmed(self.action, "stream.action"))
        object.__setattr__(
            self, "max_stream_bytes", _positive_integer(self.max_stream_bytes, "stream.max_stream_bytes")
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "SupabaseSSEProfile":
        mapping = _mapping(value, "stream")
        _reject_unknown(
            mapping,
            ("base_url", "api_key_env", "bearer_env", "path", "action", "max_stream_bytes"),
            "stream",
        )
        return cls(
            base_url=mapping.get("base_url"),
            api_key_env=mapping.get("api_key_env"),
            bearer_env=mapping.get("bearer_env"),
            path=mapping.get("path", "/functions/v1/nocode-catx-agent"),
            action=mapping.get("action", "getMessage"),
            max_stream_bytes=mapping.get("max_stream_bytes", 16 * 1024 * 1024),
        )


@dataclass(frozen=True)
class CatxRepositoryResourceProfile:
    """Repository resource defaults without persisting its authorization token."""

    url: str
    authorization_token_env: str
    mount_path: str = "/workspace/repo"

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _repository_url(self.url, "repository.url"))
        object.__setattr__(
            self,
            "authorization_token_env",
            _env_name(
                self.authorization_token_env,
                "repository.authorization_token_env",
            ),
        )
        object.__setattr__(
            self,
            "mount_path",
            _mount_path(self.mount_path, "repository.mount_path"),
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "CatxRepositoryResourceProfile":
        mapping = _mapping(value, "repository")
        _reject_unknown(
            mapping,
            ("url", "authorization_token_env", "mount_path"),
            "repository",
        )
        return cls(
            url=mapping.get("url"),
            authorization_token_env=mapping.get("authorization_token_env"),
            mount_path=mapping.get("mount_path", "/workspace/repo"),
        )

    def request_resource(self, environment: Mapping[str, str]) -> Mapping[str, str]:
        return {
            "type": "repository",
            "url": self.url,
            "authorization_token": _environment_value(
                environment,
                self.authorization_token_env,
                "CATX repository authorization token",
            ),
            "mount_path": self.mount_path,
        }


@dataclass(frozen=True)
class CatxAgentProfile:
    api_version: str
    name: str
    base_url: str
    api_key_env: str
    user_mis_id_env: str
    agent_id_env: str
    environment_id_env: str
    vault_ids: Tuple[str, ...]
    default_title: str = "aceval online evaluation"
    timeout_seconds: float = 30.0
    max_response_bytes: int = 4 * 1024 * 1024
    events_limit: int = DEFAULT_EVENTS_LIMIT
    credentials_file: Optional[str] = None
    stream: Optional[SupabaseSSEProfile] = None
    repository: Optional[CatxRepositoryResourceProfile] = None
    repositories: Tuple[CatxRepositoryResourceProfile, ...] = ()

    def __post_init__(self) -> None:
        if self.api_version != CATX_PROFILE_API_VERSION:
            raise CatxProfileError("unsupported CATX profile api_version: %s" % self.api_version)
        object.__setattr__(self, "name", _trimmed(self.name, "name"))
        object.__setattr__(self, "base_url", _base_url(self.base_url, "base_url"))
        for field_name in (
            "api_key_env",
            "user_mis_id_env",
            "agent_id_env",
            "environment_id_env",
        ):
            object.__setattr__(self, field_name, _env_name(getattr(self, field_name), field_name))
        vault_ids = tuple(self.vault_ids)
        if not vault_ids or any(not isinstance(item, str) or not item.strip() for item in vault_ids):
            raise CatxProfileError("vault_ids must contain at least one non-empty string")
        object.__setattr__(self, "vault_ids", vault_ids)
        object.__setattr__(self, "default_title", _trimmed(self.default_title, "default_title"))
        object.__setattr__(self, "timeout_seconds", _positive_number(self.timeout_seconds, "timeout_seconds"))
        object.__setattr__(
            self, "max_response_bytes", _positive_integer(self.max_response_bytes, "max_response_bytes")
        )
        limit = _positive_integer(self.events_limit, "events_limit")
        if limit > 1000:
            raise CatxProfileError("events_limit must not exceed 1000")
        object.__setattr__(self, "events_limit", limit)
        if self.credentials_file is not None:
            object.__setattr__(
                self,
                "credentials_file",
                _trimmed(self.credentials_file, "credentials_file"),
            )
        if self.stream is not None and not isinstance(self.stream, SupabaseSSEProfile):
            raise CatxProfileError("stream must be a SupabaseSSEProfile")
        if self.repository is not None and not isinstance(
            self.repository, CatxRepositoryResourceProfile
        ):
            raise CatxProfileError("repository must be a CatxRepositoryResourceProfile")
        repositories = tuple(self.repositories)
        if any(
            not isinstance(item, CatxRepositoryResourceProfile)
            for item in repositories
        ):
            raise CatxProfileError(
                "repositories must contain CatxRepositoryResourceProfile values"
            )
        if self.repository is not None and repositories:
            raise CatxProfileError(
                "CATX profile cannot configure both repository and repositories"
            )
        mount_paths = [item.mount_path for item in repositories]
        if len(mount_paths) != len(set(mount_paths)):
            raise CatxProfileError("repositories must use unique mount_path values")
        object.__setattr__(self, "repositories", repositories)

    @property
    def repository_resources(self) -> Tuple[CatxRepositoryResourceProfile, ...]:
        if self.repositories:
            return self.repositories
        return (self.repository,) if self.repository is not None else ()

    @classmethod
    def from_mapping(cls, value: Any) -> "CatxAgentProfile":
        mapping = _mapping(value, "CATX profile")
        _reject_unknown(
            mapping,
            (
                "api_version", "name", "base_url", "api_key_env", "user_mis_id_env",
                "agent_id_env", "environment_id_env", "vault_ids", "default_title",
                "timeout_seconds", "max_response_bytes", "events_limit", "stream",
                "repository", "repositories", "credentials_file",
            ),
            "CATX profile",
        )
        stream = mapping.get("stream")
        repository = mapping.get("repository")
        repositories = mapping.get("repositories", ())
        if isinstance(repositories, (str, bytes)) or not isinstance(
            repositories, Sequence
        ):
            raise CatxProfileError("repositories must be an array")
        vault_ids = mapping.get("vault_ids")
        if isinstance(vault_ids, (str, bytes)) or not isinstance(vault_ids, Sequence):
            raise CatxProfileError("vault_ids must be an array")
        return cls(
            api_version=mapping.get("api_version"),
            name=mapping.get("name"),
            base_url=mapping.get("base_url", DEFAULT_CATX_BASE_URL),
            api_key_env=mapping.get("api_key_env"),
            user_mis_id_env=mapping.get("user_mis_id_env"),
            agent_id_env=mapping.get("agent_id_env"),
            environment_id_env=mapping.get("environment_id_env"),
            vault_ids=tuple(vault_ids),
            default_title=mapping.get("default_title", "aceval online evaluation"),
            timeout_seconds=mapping.get("timeout_seconds", 30.0),
            max_response_bytes=mapping.get("max_response_bytes", 4 * 1024 * 1024),
            events_limit=mapping.get("events_limit", DEFAULT_EVENTS_LIMIT),
            credentials_file=mapping.get("credentials_file"),
            stream=SupabaseSSEProfile.from_mapping(stream) if stream is not None else None,
            repository=(
                CatxRepositoryResourceProfile.from_mapping(repository)
                if repository is not None
                else None
            ),
            repositories=tuple(
                CatxRepositoryResourceProfile.from_mapping(item)
                for item in repositories
            ),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "CatxAgentProfile":
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise CatxProfileError("cannot read CATX profile") from exc
        try:
            profile = cls.from_mapping(_strict_json(raw, "CATX profile", MAX_PROFILE_BYTES))
            if profile.credentials_file is not None:
                credentials_path = Path(profile.credentials_file)
                if not credentials_path.is_absolute():
                    credentials_path = Path(path).resolve().parent / credentials_path
                profile = replace(profile, credentials_file=str(credentials_path))
            return profile
        except ValueError as exc:
            if isinstance(exc, CatxProfileError):
                raise
            raise CatxProfileError(str(exc)) from exc


class SSETransport(Protocol):
    def stream(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_stream_bytes: int,
    ) -> Iterable[bytes]:
        ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, file_pointer: Any, code: int, message: str, headers: Any, new_url: str) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


class UrllibSSETransport:
    def __init__(self, chunk_size: int = 8192) -> None:
        self.chunk_size = _positive_integer(chunk_size, "chunk_size")
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def stream(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_stream_bytes: int,
    ) -> Iterator[bytes]:
        request = urllib.request.Request(url=url, headers=dict(headers), method="GET")
        total = 0
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                while True:
                    chunk = response.read(self.chunk_size)
                    if not chunk:
                        return
                    total += len(chunk)
                    if total > max_stream_bytes:
                        raise CatxStreamError("CATX SSE stream exceeds byte limit")
                    yield chunk
        except urllib.error.HTTPError as exc:
            raise CatxStreamError("CATX SSE returned HTTP status %d" % exc.code) from exc
        except urllib.error.URLError as exc:
            raise CatxStreamError("CATX SSE transport failed") from exc


def iter_sse_json_events(chunks: Iterable[bytes], max_bytes: int) -> Iterator[Mapping[str, Any]]:
    """Parse UTF-8 SSE frames, including chunk boundaries and multiline data."""

    decoder = __import__("codecs").getincrementaldecoder("utf-8")()
    buffer = ""
    data_lines = []
    total = 0

    def dispatch() -> Optional[Mapping[str, Any]]:
        if not data_lines:
            return None
        raw = "\n".join(data_lines).encode("utf-8")
        data_lines.clear()
        try:
            value = _strict_json(raw, "CATX SSE data", max_bytes)
        except ValueError as exc:
            raise CatxStreamError(str(exc)) from exc
        return _mapping(value, "CATX SSE event", CatxStreamError)

    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise CatxStreamError("CATX SSE transport chunks must be bytes")
        total += len(chunk)
        if total > max_bytes:
            raise CatxStreamError("CATX SSE stream exceeds byte limit")
        try:
            buffer += decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            raise CatxStreamError("CATX SSE stream is not valid UTF-8") from exc
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                event = dispatch()
                if event is not None:
                    yield event
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if field == "data":
                data_lines.append(value[1:] if separator and value.startswith(" ") else value)
    try:
        buffer += decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise CatxStreamError("CATX SSE stream is not valid UTF-8") from exc
    if buffer:
        line = buffer.rstrip("\r")
        if line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    event = dispatch()
    if event is not None:
        yield event


def _text_blocks(event: Mapping[str, Any]) -> str:
    content = event.get("content", ())
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return ""
    parts = []
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def last_effective_agent_message(events: Sequence[Mapping[str, Any]]) -> str:
    for event in reversed(tuple(events)):
        if isinstance(event, Mapping) and event.get("type") == "agent.message":
            text = _text_blocks(event)
            if text and text.strip() != "(no content)":
                return text
    return ""


def _catx_trace_event(
    event: Mapping[str, Any],
    seq: int,
    tool_names: Optional[Mapping[str, str]] = None,
) -> TraceEvent:
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise CompanyApiRequestError("CATX event type must be a non-empty string")
    kind = {
        "agent.tool_use": "tool_call",
        "agent.tool_result": "tool_result",
        "agent.message": "message",
        "agent.message.chunk": "message",
    }.get(event_type, event_type)
    name = event.get("name") if isinstance(event.get("name"), str) else event_type
    tool = name if kind == "tool_call" and name != event_type else None
    if kind == "tool_result" and tool_names is not None:
        tool_use_id = event.get("tool_use_id")
        if isinstance(tool_use_id, str):
            tool = tool_names.get(tool_use_id)
    timestamp = event.get("timestamp") or event.get("created_at") or event.get("processed_at")
    if timestamp is not None and not isinstance(timestamp, str):
        timestamp = None
    payload = {
        key: value
        for key, value in event.items()
        if key not in ("type", "name", "timestamp", "created_at", "processed_at")
    }
    error = None
    is_error = event.get("is_error")
    if kind == "tool_result" and isinstance(is_error, bool):
        payload["ok"] = not is_error
        if is_error:
            error = _text_blocks(event) or "CATX tool result reported is_error"
    elif event_type == "span.model_request_end" and is_error is True:
        error = "CATX model request reported is_error"
    elif event_type == "session.error":
        raw_error = event.get("error")
        if isinstance(raw_error, Mapping) and isinstance(raw_error.get("message"), str):
            error = raw_error["message"]
        elif isinstance(raw_error, str):
            error = raw_error
        else:
            error = "CATX session error"
    return TraceEvent(
        kind=kind,
        seq=seq,
        timestamp=timestamp,
        name=name,
        tool=tool,
        payload=payload,
        error=error,
    )


def _catx_trace(events: Sequence[Mapping[str, Any]]) -> Tuple[TraceEvent, ...]:
    tool_names = {}
    for event in events:
        if event.get("type") != "agent.tool_use":
            continue
        event_id = event.get("id")
        name = event.get("name")
        if isinstance(event_id, str) and isinstance(name, str) and name:
            tool_names[event_id] = name
    return tuple(
        _catx_trace_event(event, index, tool_names)
        for index, event in enumerate(events)
    )


def _catx_usage(value: Any) -> Mapping[str, Any]:
    """Flatten CATX/Anthropic cache telemetry into numeric usage fields."""

    if not isinstance(value, Mapping):
        raise CompanyApiRequestError("CATX status usage must be an object")
    usage = {}
    cache_creation_total = 0
    for key, item in value.items():
        if not isinstance(key, str):
            raise CompanyApiRequestError("CATX usage keys must be strings")
        if key == "cache_creation":
            if not isinstance(item, Mapping):
                raise CompanyApiRequestError("CATX cache_creation usage must be an object")
            for cache_key, cache_value in item.items():
                if not isinstance(cache_key, str):
                    raise CompanyApiRequestError("CATX cache_creation usage keys must be strings")
                if (
                    isinstance(cache_value, bool)
                    or not isinstance(cache_value, int)
                    or cache_value < 0
                ):
                    raise CompanyApiRequestError(
                        "CATX cache_creation token values must be non-negative integers"
                    )
                usage["cache_creation_%s" % cache_key] = cache_value
                cache_creation_total += cache_value
            usage["cache_creation_input_tokens"] = cache_creation_total
            continue
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or item < 0
        ):
            raise CompanyApiRequestError("CATX usage values must be non-negative finite numbers")
        if key.endswith("_tokens") and not isinstance(item, int):
            raise CompanyApiRequestError("CATX token usage values must be integers")
        usage[key] = item
    if "total_tokens" not in usage:
        total = sum(
            int(usage.get(key, 0))
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
        if total or any(key in usage for key in ("input_tokens", "output_tokens")):
            usage["total_tokens"] = total
    return usage


class CatxAgentClient:
    """Creates CATX sessions and imports their online observations."""

    def __init__(
        self,
        profile: CatxAgentProfile,
        *,
        transport: Optional[HTTPTransport] = None,
        sse_transport: Optional[SSETransport] = None,
        environment: Optional[Mapping[str, str]] = None,
        binding_adapter: Optional[CatxSessionBindingAdapter] = None,
    ) -> None:
        if not isinstance(profile, CatxAgentProfile):
            raise TypeError("profile must be a CatxAgentProfile")
        if environment is not None and not isinstance(environment, Mapping):
            raise TypeError("environment must be a mapping")
        self.profile = profile
        self._transport = transport or UrllibHTTPTransport()
        self._sse_transport = sse_transport or UrllibSSETransport()
        allowed_names = {
            profile.api_key_env,
            profile.user_mis_id_env,
            profile.agent_id_env,
            profile.environment_id_env,
        }
        for repository in profile.repository_resources:
            allowed_names.add(repository.authorization_token_env)
        if profile.stream is not None:
            allowed_names.add(profile.stream.api_key_env)
            if profile.stream.bearer_env is not None:
                allowed_names.add(profile.stream.bearer_env)
        effective_environment = {}
        if profile.credentials_file is not None:
            effective_environment.update(
                _load_credentials_file(profile.credentials_file, allowed_names)
            )
        overrides = environment if environment is not None else os.environ
        for name in allowed_names:
            if name in overrides:
                effective_environment[name] = overrides[name]
        self._environment = effective_environment
        self._sensitive_values = tuple(
            sorted(
                {
                    value
                    for value in effective_environment.values()
                    if isinstance(value, str) and len(value) >= 4
                },
                key=len,
                reverse=True,
            )
        )
        self._binding_adapter = binding_adapter
        self.last_binding_evidence: Optional[CatxBindingEvidence] = None
        self._event_logs: dict[Tuple[str, Optional[str]], Mapping[str, Any]] = {}

    def _redact_event(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
        redacted = _redact_sensitive(event, self._sensitive_values)
        return _mapping(redacted, "redacted CATX event", CompanyApiRequestError)

    def _headers(self) -> Mapping[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "aceval-catx-connector/1",
            "X-Api-Key": _environment_value(self._environment, self.profile.api_key_env, "CATX API key"),
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "files-api-2025-04-14",
            "user-mis-id": _environment_value(self._environment, self.profile.user_mis_id_env, "CATX user MIS"),
        }

    def _request_document(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        body = None
        if payload is not None:
            try:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeError) as exc:
                raise CompanyApiRequestError("CATX request is not strict JSON") from exc
            if len(body) > self.profile.max_response_bytes:
                raise CompanyApiRequestError("CATX request exceeds byte limit")
        try:
            response = self._transport.request(
                method=method,
                url=self.profile.base_url + path,
                headers=self._headers(),
                body=body,
                timeout_seconds=self.profile.timeout_seconds,
                max_response_bytes=self.profile.max_response_bytes,
            )
        except CompanyApiRequestError:
            raise
        except Exception as exc:
            raise CompanyApiRequestError("CATX API transport failed") from exc
        if not isinstance(response, HTTPResponse):
            raise CompanyApiRequestError("CATX API transport returned an invalid response")
        if not 200 <= response.status_code < 300:
            detail = ""
            try:
                error_document = _strict_json(
                    response.body,
                    "CATX API error response",
                    self.profile.max_response_bytes,
                )
                redacted = _redact_sensitive(error_document, self._sensitive_values)
                if isinstance(redacted, Mapping):
                    candidate = (
                        redacted.get("message")
                        or redacted.get("detail")
                        or redacted.get("error")
                    )
                    if isinstance(candidate, Mapping):
                        candidate = candidate.get("message") or candidate.get("detail")
                    if isinstance(candidate, str) and candidate.strip():
                        detail = ": " + candidate.strip()[:1000]
                elif isinstance(redacted, str) and redacted.strip():
                    detail = ": " + redacted.strip()[:1000]
            except ValueError:
                pass
            raise CompanyApiRequestError(
                "CATX API returned HTTP status %d%s"
                % (response.status_code, detail)
            )
        try:
            value = _strict_json(response.body, "CATX API response", self.profile.max_response_bytes)
        except ValueError as exc:
            raise CompanyApiRequestError(str(exc)) from exc
        return value

    def _request_json(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        return _mapping(
            self._request_document(method, path, payload),
            "CATX API response",
            CompanyApiRequestError,
        )

    def start_session(
        self,
        request: Mapping[str, Any],
        *,
        binding: Optional[CatxExecutionBinding] = None,
    ) -> str:
        request = _mapping(request, "CATX start request", CompanyApiRequestError)
        unknown = sorted(set(request) - {"prompt", "title"})
        if unknown:
            raise CompanyApiRequestError("CATX start request contains unsupported fields: %s" % ", ".join(unknown))
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
            raise CompanyApiRequestError("CATX start request prompt must be a non-empty string")
        title = request.get("title", self.profile.default_title)
        if (
            not isinstance(title, str)
            or not title.strip()
            or title != title.strip()
            or any(ord(character) < 32 for character in title)
        ):
            raise CompanyApiRequestError("CATX start request title must be a trimmed non-empty string")
        create_payload = {
            "title": title,
            "agent": _environment_value(self._environment, self.profile.agent_id_env, "CATX Agent ID"),
            "environment_id": _environment_value(self._environment, self.profile.environment_id_env, "CATX Environment ID"),
            "vault_ids": list(self.profile.vault_ids),
        }
        # CATX mounts repositories through the documented `resources` field.
        # Keep the resource materialization in the connector so every session
        # (including the first message) sees the same immutable mounts; tokens
        # are resolved only in memory and never persisted in the profile.
        if self.profile.repository_resources:
            create_payload["resources"] = [
                repository.request_resource(self._environment)
                for repository in self.profile.repository_resources
            ]
        if binding is not None:
            if not isinstance(binding, CatxExecutionBinding):
                raise TypeError("binding must be a CatxExecutionBinding")
            if (
                self.profile.repository_resources
                and binding.repository_ref
                not in {item.url for item in self.profile.repository_resources}
            ):
                raise CompanyApiRequestError(
                    "CATX binding repository_ref does not match configured repository.url"
                )
            if self._binding_adapter is None:
                raise CompanyApiRequestError(
                    "CATX exact Skill/repository binding requires a binding adapter"
                )
            augmented = self._binding_adapter.augment_create_payload(create_payload, binding)
            if not isinstance(augmented, Mapping):
                raise CompanyApiRequestError("CATX binding adapter returned an invalid payload")
            # Existing profile-owned fields are immutable. The adapter may only
            # add server-defined binding/mount fields.
            changed = [key for key, value in create_payload.items() if augmented.get(key) != value]
            if changed:
                raise CompanyApiRequestError(
                    "CATX binding adapter changed protected fields: %s" % ", ".join(changed)
                )
            create_payload = dict(augmented)
        create = self._request_json("POST", "/sessions", create_payload)
        session_id = _safe_session_id(create.get("id"))
        if binding is not None and getattr(self._binding_adapter, "verify_before_message", False):
            # Binding must be proven immediately after creation and before the
            # first user message is appended.  A session with unverifiable
            # repository provenance is never allowed to execute.
            try:
                evidence = self.verify_binding(session_id, binding)
                evidence.assert_matches(binding)
            except Exception as exc:
                raise CompanyApiRequestError(
                    "CATX session %s was created but repository binding verification failed: %s" % (session_id, exc)
                ) from exc
            self.last_binding_evidence = evidence
        encoded = urllib.parse.quote(session_id, safe="")
        try:
            self._request_json(
                "POST",
                "/sessions/%s/events" % encoded,
                {"events": [{"type": "user.message", "content": [{"type": "text", "text": prompt}]}]},
            )
        except CompanyApiRequestError as exc:
            raise CompanyApiRequestError(
                "CATX session %s was created but the initial message failed: %s" % (session_id, exc)
            ) from exc
        return session_id

    def verify_binding(
        self,
        session_id: str,
        binding: CatxExecutionBinding,
    ) -> CatxBindingEvidence:
        session = _safe_session_id(session_id)
        if not isinstance(binding, CatxExecutionBinding):
            raise TypeError("binding must be a CatxExecutionBinding")
        if self._binding_adapter is None:
            raise CompanyApiRequestError(
                "CATX exact Skill/repository binding requires a binding adapter"
            )
        evidence = self._binding_adapter.verify_session(self, session, binding)
        if not isinstance(evidence, CatxBindingEvidence):
            raise CompanyApiRequestError("CATX binding adapter returned invalid evidence")
        return evidence

    def verify_binding_events(
        self,
        session_id: str,
        binding: CatxExecutionBinding,
        events: Sequence[Mapping[str, Any]],
    ) -> CatxBindingEvidence:
        """Upgrade request-level binding evidence using the complete event log."""
        session = _safe_session_id(session_id)
        if not isinstance(binding, CatxExecutionBinding):
            raise TypeError("binding must be a CatxExecutionBinding")
        if self._binding_adapter is None:
            raise CompanyApiRequestError("CATX exact Skill/repository binding requires a binding adapter")
        verifier = getattr(self._binding_adapter, "verify_event_log", None)
        if not callable(verifier):
            raise CompanyApiRequestError("CATX binding adapter cannot verify event logs")
        evidence = verifier(self, session, binding, events)
        if not isinstance(evidence, CatxBindingEvidence):
            raise CompanyApiRequestError("CATX binding adapter returned invalid event evidence")
        self.last_binding_evidence = evidence
        return evidence

    def get_status(self, session_id: str) -> Mapping[str, Any]:
        session = _safe_session_id(session_id)
        encoded = urllib.parse.quote(session, safe="")
        return self._request_json("GET", "/sessions/%s" % encoded)

    @staticmethod
    def _page_cursor(response: Mapping[str, Any]) -> Tuple[Optional[str], Optional[bool], Optional[int]]:
        containers = [response]
        for key in ("pagination", "page", "meta"):
            value = response.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
        next_cursor = None
        has_more = None
        total = None
        for value in containers:
            if next_cursor is None:
                candidate = value.get("next_cursor", value.get("nextCursor"))
                if isinstance(candidate, str) and candidate:
                    next_cursor = candidate
            if has_more is None:
                candidate = value.get("has_more", value.get("hasMore"))
                if isinstance(candidate, bool):
                    has_more = candidate
            if total is None:
                candidate = value.get("total")
                if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                    total = candidate
        return next_cursor, has_more, total

    @staticmethod
    def _event_log_integrity(
        events: Sequence[Mapping[str, Any]],
        *,
        page_count: int,
        pagination_complete: bool,
        declared_total: Optional[int],
    ) -> Mapping[str, Any]:
        reasons = []
        if not pagination_complete:
            reasons.append("pagination_not_proven_complete")
        if declared_total is not None and declared_total != len(events):
            reasons.append("declared_total_mismatch")

        sequences = []
        sequence_missing = False
        for event in events:
            raw = event.get("seq", event.get("sequence"))
            if raw is None:
                sequence_missing = True
                continue
            if isinstance(raw, bool) or not isinstance(raw, int):
                reasons.append("invalid_sequence")
                continue
            sequences.append(raw)
        sequence_status = "not_provided"
        if sequences:
            sequence_status = "complete"
            if sequence_missing:
                sequence_status = "partial"
                reasons.append("partial_sequence_numbers")
            if len(sequences) != len(set(sequences)):
                sequence_status = "duplicate"
                reasons.append("duplicate_sequence")
            ordered = sorted(set(sequences))
            if ordered and ordered != list(range(ordered[0], ordered[-1] + 1)):
                sequence_status = "gap"
                reasons.append("sequence_gap")

        event_ids = [
            str(event.get("event_id"))
            for event in events
            if isinstance(event.get("event_id"), str) and event.get("event_id")
        ]
        if len(event_ids) != len(set(event_ids)):
            reasons.append("duplicate_event_id")

        calls = set()
        results = set()
        for event in events:
            kind = str(event.get("type") or event.get("kind") or "")
            if kind in ("agent.tool_use", "tool.call", "tool_call"):
                call_id = event.get("id") or event.get("tool_use_id") or event.get("tool_call_id")
                if call_id:
                    calls.add(str(call_id))
            elif kind in ("agent.tool_result", "tool.result", "tool_result"):
                call_id = event.get("tool_use_id") or event.get("tool_call_id") or event.get("parent_event_id")
                if call_id:
                    results.add(str(call_id))
        unmatched_calls = sorted(calls.difference(results))
        orphan_results = sorted(results.difference(calls))
        if unmatched_calls:
            reasons.append("tool_result_missing")
        if orphan_results:
            reasons.append("orphan_tool_result")

        required = ("user.message", "agent.message")
        event_types = sorted(
            {str(event.get("type") or event.get("kind") or "") for event in events}
        )
        missing_required = [kind for kind in required if kind not in event_types]
        if missing_required:
            reasons.append("required_event_type_missing")
        canonical = json.dumps(
            list(events),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return {
            "complete": not reasons,
            "reason_codes": list(dict.fromkeys(reasons)),
            "page_count": page_count,
            "event_count": len(events),
            "declared_total": declared_total,
            "event_types": event_types,
            "required_event_types": list(required),
            "missing_required_event_types": missing_required,
            "sequence_status": sequence_status,
            "unmatched_tool_call_ids": unmatched_calls,
            "orphan_tool_result_ids": orphan_results,
            "event_log_sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def fetch_events(self, session_id: str, *, event_type: Optional[str] = None) -> Tuple[Mapping[str, Any], ...]:
        session = _safe_session_id(session_id)
        encoded = urllib.parse.quote(session, safe="")
        normalized_type = _trimmed(event_type, "event_type") if event_type is not None else None
        cursor = None
        seen_cursors = set()
        pages = 0
        all_events = []
        declared_total = None
        pagination_complete = False
        while True:
            query = {"order": "asc", "limit": str(self.profile.events_limit)}
            if normalized_type is not None:
                query["eventType"] = normalized_type
            if cursor is not None:
                query["cursor"] = cursor
            path = "/sessions/%s/events?%s" % (encoded, urllib.parse.urlencode(query))
            response = self._request_json("GET", path)
            page_events = response.get("data")
            if not isinstance(page_events, list) or any(not isinstance(item, Mapping) for item in page_events):
                raise CompanyApiRequestError("CATX events response data must be an array of objects")
            all_events.extend(dict(self._redact_event(item)) for item in page_events)
            pages += 1
            next_cursor, has_more, total = self._page_cursor(response)
            if total is not None:
                if declared_total is not None and total != declared_total:
                    raise CompanyApiRequestError("CATX events pagination total changed between pages")
                declared_total = total
            if has_more is True or next_cursor is not None:
                if not next_cursor:
                    raise CompanyApiRequestError("CATX events page declares more data without a cursor")
                if next_cursor in seen_cursors or pages >= 100:
                    raise CompanyApiRequestError("CATX events pagination cursor did not make progress")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
                continue
            # A short legacy page is complete. A full page is also complete
            # when the server-declared total exactly matches the events already
            # collected; otherwise missing cursor metadata remains ambiguous.
            pagination_complete = (
                has_more is False
                or len(page_events) < self.profile.events_limit
                or (declared_total is not None and declared_total == len(all_events))
            )
            break
        events = tuple(all_events)
        integrity = self._event_log_integrity(
            events,
            page_count=pages,
            pagination_complete=pagination_complete,
            declared_total=declared_total,
        )
        self._event_logs[(session, normalized_type)] = {
            "events": events,
            "integrity": integrity,
        }
        return events

    def last_event_log(
        self, session_id: str, *, event_type: Optional[str] = None
    ) -> Optional[Mapping[str, Any]]:
        session = _safe_session_id(session_id)
        normalized_type = _trimmed(event_type, "event_type") if event_type is not None else None
        value = self._event_logs.get((session, normalized_type))
        if value is None:
            return None
        return {
            "events": tuple(dict(item) for item in value.get("events", ())),
            "integrity": dict(value.get("integrity", {})),
        }

    def poll_session(self, session_id: str) -> Mapping[str, Any]:
        status_document = self.get_status(session_id)
        status = status_document.get("status")
        if not isinstance(status, str) or not status:
            raise CompanyApiRequestError("CATX status response is missing status")
        if status in ("running", "rescheduling"):
            return {"status": "RUNNING", "round_count": 0, "message": None, "usage": status_document.get("usage")}
        if status in ("idle", "terminated"):
            events = self.fetch_events(session_id)
            message = last_effective_agent_message(events)
            latest_user_index = max(
                (
                    index
                    for index, event in enumerate(events)
                    if event.get("type") == "user.message"
                ),
                default=-1,
            )
            latest_terminal_index = max(
                (
                    index
                    for index, event in enumerate(events)
                    if event.get("type")
                    in ("session.status_idle", "session.error")
                ),
                default=-1,
            )
            session_error = None
            for event in reversed(events):
                if event.get("type") != "session.error":
                    continue
                raw_error = event.get("error")
                if isinstance(raw_error, Mapping):
                    session_error = raw_error.get("message") or raw_error.get("type")
                elif isinstance(raw_error, str):
                    session_error = raw_error
                session_error = str(session_error or "CATX session error")
                break
            if (
                status == "idle"
                and not message
                and latest_user_index >= 0
                and latest_terminal_index < latest_user_index
            ):
                return {
                    "status": "RUNNING",
                    "round_count": 0,
                    "message": None,
                    "error": None,
                    "usage": status_document.get("usage"),
                    "catx_status": status,
                }
            if session_error is not None or (status == "terminated" and not message):
                mapped = "FAILED"
            else:
                mapped = "COMPLETED"
            return {
                "status": mapped,
                "round_count": 1 if message else 0,
                "message": message or None,
                "error": session_error,
                "usage": status_document.get("usage"),
                "catx_status": status,
            }
        return {"status": "RUNNING", "round_count": 0, "message": None, "usage": status_document.get("usage"), "catx_status": status}

    def fetch_session(self, session_id: str) -> ImportedRunBundle:
        session = _safe_session_id(session_id)
        status_document = self.get_status(session)
        events = self.fetch_events(session)
        event_log = self.last_event_log(session) or {}
        event_integrity = (
            event_log.get("integrity", {})
            if isinstance(event_log.get("integrity"), Mapping)
            else {}
        )
        output = last_effective_agent_message(events)
        status = status_document.get("status")
        if not isinstance(status, str) or not status:
            raise CompanyApiRequestError("CATX status response is missing status")
        error = None
        for event in reversed(events):
            if event.get("type") == "session.error":
                raw_error = event.get("error")
                if isinstance(raw_error, Mapping) and isinstance(raw_error.get("message"), str):
                    error = raw_error["message"]
                elif isinstance(raw_error, str):
                    error = raw_error
                else:
                    error = "CATX session error"
                break
        if error is None and status == "terminated" and not output:
            error = "CATX session terminated without an effective agent message"
        usage = _catx_usage(status_document.get("usage", {}))
        trace = _catx_trace(events)
        truncated = event_integrity.get("complete") is not True
        expected_fields = frozenset(("output", "trace", "usage", "error", "metadata"))
        observed_fields = expected_fields if not truncated else frozenset(("output", "usage", "error", "metadata"))
        completeness = ObservationCompleteness(expected=expected_fields, observed=observed_fields)
        observation = RunObservation(
            output=output,
            trace=trace,
            error=error,
            usage=usage,
            metadata={
                "source": "catx_session_api",
                "profile_name": self.profile.name,
                "session_id": session,
                "catx_status": status,
                "events_limit": self.profile.events_limit,
                "trace_may_be_truncated": truncated,
                "raw_event_count": len(events),
                "required_event_types": event_integrity.get("required_event_types", ["user.message", "agent.message"]),
                "missing_required_event_types": event_integrity.get("missing_required_event_types", []),
                "event_log_integrity": event_integrity,
                "observation_completeness": completeness.as_dict(),
            },
        )
        return ImportedRunBundle(
            session_id=session,
            profile_name=self.profile.name,
            observation=observation,
            completeness=completeness,
            source="catx_session_api",
        )

    def listen_session(
        self,
        session_id: str,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> ImportedRunBundle:
        stream = self.profile.stream
        if stream is None:
            raise CatxStreamError("CATX profile does not configure a Supabase SSE stream")
        session = _safe_session_id(session_id)
        api_key = _environment_value(self._environment, stream.api_key_env, "Supabase API key")
        bearer = api_key
        if stream.bearer_env is not None and self._environment.get(stream.bearer_env):
            bearer = _environment_value(self._environment, stream.bearer_env, "Supabase bearer token")
        query = urllib.parse.urlencode({"action": stream.action, "session_id": session})
        url = stream.base_url + stream.path + "?" + query
        headers = {
            "Accept": "text/event-stream",
            "Authorization": "Bearer " + bearer,
            "apikey": api_key,
            "User-Agent": "aceval-catx-connector/1",
        }
        chunks = self._sse_transport.stream(
            url=url,
            headers=headers,
            timeout_seconds=self.profile.timeout_seconds,
            max_stream_bytes=stream.max_stream_bytes,
        )
        terminal = False
        stream_error: Optional[str] = None
        try:
            for event in iter_sse_json_events(chunks, stream.max_stream_bytes):
                event = self._redact_event(event)
                if on_event is not None:
                    on_event(event)
                event_type = event.get("type")
                if event_type == "session.error":
                    terminal = True
                    raw_error = event.get("error")
                    message = raw_error.get("message") if isinstance(raw_error, Mapping) else raw_error
                    stream_error = str(message or "CATX SSE reported a session error")
                    break
                if event_type == "session.status_idle":
                    terminal = True
                    break
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                close()
        if not terminal:
            raise CatxStreamError("CATX SSE ended before a terminal session event")
        # Fetch the authoritative status and all event types after the stream
        # terminal marker so downstream diagnosis receives the complete log.
        bundle = self.fetch_session(session)
        if stream_error is not None and (
            bundle.observation.error is None
            or bundle.observation.error
            == "CATX session terminated without an effective agent message"
        ):
            bundle = replace(
                bundle,
                observation=replace(bundle.observation, error=stream_error),
            )
        return bundle


__all__ = [
    "CATX_PROFILE_API_VERSION",
    "CatxProfileError",
    "CatxStreamError",
    "CatxRepositoryResourceProfile",
    "SupabaseSSEProfile",
    "CatxAgentProfile",
    "SSETransport",
    "UrllibSSETransport",
    "iter_sse_json_events",
    "last_effective_agent_message",
    "CatxAgentClient",
]
