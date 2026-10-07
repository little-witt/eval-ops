"""Project-owned reference agent loop with a small, auditable tool surface."""

from __future__ import annotations

import json
import hashlib
import math
import os
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence


class ReferenceRuntimeError(RuntimeError):
    """Raised when the reference runtime cannot produce a valid run."""

    def __init__(
        self,
        message: str,
        *,
        usage: Optional[Mapping[str, Any]] = None,
        trace: Sequence[Mapping[str, Any]] = (),
        duration_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.usage = dict(usage or {})
        self.trace = tuple(trace)
        self.duration_seconds = float(duration_seconds)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class ModelReply:
    content: str = ""
    tool_calls: Sequence[ToolCall] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    # Provider bridges may report the concrete model selected after resolving
    # a CC Switch alias. Existing clients can omit this field.
    model_id: Optional[str] = None


class ModelClient(Protocol):
    """Thin inference boundary; it is not an Agent platform adapter."""

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelReply:
        ...


@dataclass(frozen=True)
class ReferenceRunResult:
    final_output: str
    trace: Sequence[Mapping[str, Any]]
    usage: Mapping[str, Any]
    duration_seconds: float


@dataclass(frozen=True)
class ReferenceRuntimeConfig:
    max_steps: int = 8
    max_read_bytes: int = 64 * 1024
    max_write_bytes: int = 256 * 1024
    max_trace_events: int = 200
    max_tool_calls: int = 64

    def __post_init__(self) -> None:
        for name in (
            "max_steps",
            "max_tool_calls",
            "max_read_bytes",
            "max_write_bytes",
            "max_trace_events",
        ):
            _require_positive_integer(name, getattr(self, name))


def _require_positive_integer(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("%s must be a positive integer" % name)


def _require_positive_number(name: str, value: Any) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
    ):
        raise ValueError("%s must be a positive number" % name)
    try:
        finite = math.isfinite(float(value))
    except OverflowError as exc:
        raise ValueError("%s must be a positive number" % name) from exc
    if not finite:
        raise ValueError("%s must be a positive number" % name)


def _validated_usage(value: Any, error_message: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReferenceRuntimeError(error_message)
    usage = dict(value)
    token_fields = {
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
    }
    for key, item in usage.items():
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ReferenceRuntimeError(error_message)
        if item < 0 or (isinstance(item, float) and not math.isfinite(item)):
            raise ReferenceRuntimeError(error_message)
        if key in token_fields and not isinstance(item, int):
            raise ReferenceRuntimeError(error_message)
    return usage


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


class CommandModelClient:
    """Calls a model bridge over a stable JSON stdin/stdout envelope.

    The command receives ``{"messages": [...], "tools": [...]}`` on stdin and
    returns ``{"content": "...", "tool_calls": [...], "usage": {...}}``.
    It is passed as argv and never through a shell.
    """

    def __init__(
        self,
        argv: Sequence[str],
        timeout_seconds: float = 120,
        env_allowlist: Iterable[str] = (),
        max_request_bytes: int = 4 * 1024 * 1024,
        max_stdout_bytes: int = 1024 * 1024,
        max_stderr_bytes: int = 64 * 1024,
        model_id: Optional[str] = None,
    ) -> None:
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("model bridge argv must not be empty")
        if any(not isinstance(item, str) for item in argv) or not argv[0]:
            raise ValueError("model bridge argv must contain strings")
        _require_positive_number("timeout_seconds", timeout_seconds)
        _require_positive_integer("max_request_bytes", max_request_bytes)
        _require_positive_integer("max_stdout_bytes", max_stdout_bytes)
        _require_positive_integer("max_stderr_bytes", max_stderr_bytes)
        self._argv = tuple(argv)
        self._timeout_seconds = float(timeout_seconds)
        self._env_allowlist = tuple(env_allowlist)
        self._max_request_bytes = max_request_bytes
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._model_id = str(model_id) if model_id else None

    @property
    def profile(self) -> Mapping[str, Any]:
        argv_payload = json.dumps(
            list(self._argv),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        environment_presence = {
            name: name in os.environ for name in sorted(set(self._env_allowlist))
        }
        environment_hash = hashlib.sha256(
            json.dumps(
                environment_presence,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "protocol_version": "aceval.model-bridge/v1",
            "model_id": self._model_id,
            "executable": self._argv[0],
            "argv_count": len(self._argv),
            "argv_sha256": hashlib.sha256(argv_payload).hexdigest(),
            "timeout_seconds": self._timeout_seconds,
            "max_request_bytes": self._max_request_bytes,
            "max_stdout_bytes": self._max_stdout_bytes,
            "max_stderr_bytes": self._max_stderr_bytes,
            "env_allowlist": tuple(sorted(set(self._env_allowlist))),
            "environment_presence_hash": environment_hash,
        }

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelReply:
        try:
            request = json.dumps(
                {"messages": messages, "tools": tools},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReferenceRuntimeError("invalid model bridge request") from exc
        if len(request) > self._max_request_bytes:
            raise ReferenceRuntimeError("model bridge request exceeds byte limit")
        env = {name: os.environ[name] for name in self._env_allowlist if name in os.environ}
        env.setdefault("PATH", os.environ.get("PATH", ""))
        response = self._invoke(request, env)
        return self._parse_response(response)

    def _invoke(self, request: bytes, env: Mapping[str, str]) -> bytes:
        with ExitStack() as stack:
            request_stream = stack.enter_context(tempfile.TemporaryFile())
            stdout_stream = stack.enter_context(tempfile.TemporaryFile())
            stderr_stream = stack.enter_context(tempfile.TemporaryFile())
            request_stream.write(request)
            request_stream.seek(0)
            try:
                process = subprocess.Popen(
                    self._argv,
                    stdin=request_stream,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    env=dict(env),
                )
            except OSError as exc:
                raise ReferenceRuntimeError("model bridge could not start") from exc

            deadline = time.monotonic() + self._timeout_seconds
            while process.poll() is None:
                exceeded = self._exceeded_stream_limit(stdout_stream, stderr_stream)
                if exceeded is not None:
                    self._stop_process(process)
                    raise ReferenceRuntimeError(
                        "model bridge %s exceeds byte limit" % exceeded
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop_process(process)
                    raise ReferenceRuntimeError("model bridge timed out")
                try:
                    process.wait(timeout=min(0.025, remaining))
                except subprocess.TimeoutExpired:
                    pass

            exceeded = self._exceeded_stream_limit(stdout_stream, stderr_stream)
            if exceeded is not None:
                raise ReferenceRuntimeError("model bridge %s exceeds byte limit" % exceeded)
            if process.returncode != 0:
                stderr_stream.seek(0)
                stderr = stderr_stream.read(self._max_stderr_bytes + 1)
                detail = stderr.decode("utf-8", errors="replace").strip()
                # Keep the user-facing failure actionable while bounding the
                # amount of subprocess output persisted in task events.
                if len(detail) > 2000:
                    detail = detail[-2000:]
                message = "model bridge exited with status %s" % process.returncode
                if detail:
                    message = "%s: %s" % (message, detail)
                raise ReferenceRuntimeError(message)
            stdout_stream.seek(0)
            return stdout_stream.read(self._max_stdout_bytes + 1)

    def _exceeded_stream_limit(self, stdout_stream: Any, stderr_stream: Any) -> Optional[str]:
        if os.fstat(stdout_stream.fileno()).st_size > self._max_stdout_bytes:
            return "stdout"
        if os.fstat(stderr_stream.fileno()).st_size > self._max_stderr_bytes:
            return "stderr"
        return None

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.wait()

    @staticmethod
    def _parse_response(response: bytes) -> ModelReply:
        try:
            payload = json.loads(
                response.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
            if not isinstance(payload, dict):
                raise TypeError("response must be an object")
            content = payload["content"]
            raw_calls = payload["tool_calls"]
            raw_usage = payload["usage"]
            if not isinstance(content, str):
                raise TypeError("content must be a string")
            if not isinstance(raw_calls, list):
                raise TypeError("tool_calls must be an array")
            if not isinstance(raw_usage, dict):
                raise TypeError("usage must be an object")

            calls = []
            call_ids = set()
            for item in raw_calls:
                if not isinstance(item, dict):
                    raise TypeError("tool call must be an object")
                name = item["name"]
                arguments = item["arguments"]
                call_id = item["id"]
                if not isinstance(name, str) or not name.strip():
                    raise TypeError("tool call name must be a non-empty string")
                if not isinstance(arguments, dict):
                    raise TypeError("tool call arguments must be an object")
                if not isinstance(call_id, str) or not call_id:
                    raise TypeError("tool call id must be a non-empty string")
                if call_id in call_ids:
                    raise ValueError("duplicate tool call id")
                call_ids.add(call_id)
                calls.append(ToolCall(name=name, arguments=arguments, call_id=call_id))

            return ModelReply(
                content=content,
                tool_calls=tuple(calls),
                usage=_validated_usage(raw_usage, "invalid model bridge response"),
                model_id=(str(payload.get("model_id")) if payload.get("model_id") else None),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
        ) as exc:
            raise ReferenceRuntimeError("invalid model bridge response") from exc


class ScriptedModelClient:
    """Deterministic model client used by conformance and unit tests."""

    def __init__(self, replies: Sequence[ModelReply]) -> None:
        self._replies = list(replies)

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelReply:
        del messages, tools
        if not self._replies:
            raise ReferenceRuntimeError("scripted model has no reply left")
        return self._replies.pop(0)


class ReferenceAgentRuntime:
    """Runs a Skill through one frozen agent loop and restricted file tools."""

    def __init__(
        self,
        model_client: ModelClient,
        config: Optional[ReferenceRuntimeConfig] = None,
    ) -> None:
        self._model = model_client
        self._config = config or ReferenceRuntimeConfig()

    @property
    def profile(self) -> Mapping[str, Any]:
        model_profile = getattr(self._model, "profile", None)
        if callable(model_profile):
            model_profile = model_profile()
        return {
            "config": {
                "max_steps": self._config.max_steps,
                "max_read_bytes": self._config.max_read_bytes,
                "max_write_bytes": self._config.max_write_bytes,
                "max_trace_events": self._config.max_trace_events,
                "max_tool_calls": self._config.max_tool_calls,
            },
            "model_bridge": (
                dict(model_profile) if isinstance(model_profile, Mapping) else None
            ),
        }

    def run(
        self,
        skill_path: Path,
        prompt: str,
        workspace: Path,
        max_tool_calls: Optional[int] = None,
    ) -> ReferenceRunResult:
        if max_tool_calls is not None:
            _require_positive_integer("max_tool_calls", max_tool_calls)
        tool_call_limit = min(
            self._config.max_tool_calls,
            max_tool_calls if max_tool_calls is not None else self._config.max_tool_calls,
        )
        started = time.monotonic()
        root = workspace.resolve()
        root.mkdir(parents=True, exist_ok=True)
        skill_file = self._resolve_skill_file(skill_path)
        instruction = skill_file.read_text(encoding="utf-8")
        messages: List[Mapping[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are running one frozen Agent Skill. Follow the Skill instructions, "
                    "use only the declared tools, and return the task result without discussing "
                    "the evaluation system.\n\n<skill>\n%s\n</skill>" % instruction
                ),
            },
            {"role": "user", "content": prompt},
        ]
        trace: List[Mapping[str, Any]] = []
        usage: Dict[str, Any] = {}
        seen_tool_call_ids = set()
        tool_call_count = 0

        try:
            for step in range(self._config.max_steps):
                self._append_trace(trace, "model_call", {"step": step})
                reply = self._validated_reply(
                    self._model.complete(messages, self.tool_specs())
                )
                # The model call has already happened, so account for it even
                # when its tool batch is rejected before execution.
                self._merge_usage(usage, reply.usage)
                reply_call_ids = {call.call_id for call in reply.tool_calls}
                if len(reply_call_ids) != len(
                    reply.tool_calls
                ) or seen_tool_call_ids.intersection(reply_call_ids):
                    raise ReferenceRuntimeError("model returned duplicate tool call id")
                tool_call_count += len(reply.tool_calls)
                if tool_call_count > tool_call_limit:
                    raise ReferenceRuntimeError(
                        "reference runtime exceeded max_tool_calls"
                    )
                seen_tool_call_ids.update(reply_call_ids)
                assistant_message: Dict[str, Any] = {
                    "role": "assistant",
                    "content": reply.content,
                }
                if reply.tool_calls:
                    assistant_message["tool_calls"] = [
                        {
                            "id": call.call_id,
                            "name": call.name,
                            "arguments": dict(call.arguments),
                        }
                        for call in reply.tool_calls
                    ]
                messages.append(assistant_message)

                if not reply.tool_calls:
                    if not reply.content:
                        raise ReferenceRuntimeError(
                            "model returned neither content nor tool calls"
                        )
                    self._append_trace(
                        trace,
                        "message",
                        {"role": "assistant", "content": reply.content},
                    )
                    return ReferenceRunResult(
                        final_output=reply.content,
                        trace=tuple(trace),
                        usage=usage,
                        duration_seconds=time.monotonic() - started,
                    )

                for call in reply.tool_calls:
                    self._append_trace(
                        trace,
                        "tool_call",
                        {
                            "id": call.call_id,
                            "name": call.name,
                            "arguments": dict(call.arguments),
                        },
                    )
                    try:
                        result = self._execute_tool(call, root)
                        tool_payload: Mapping[str, Any] = {
                            "ok": True,
                            "result": result,
                        }
                    except (OSError, ValueError, ReferenceRuntimeError) as exc:
                        tool_payload = {"ok": False, "error": str(exc)}
                    self._append_trace(
                        trace,
                        "tool_result",
                        {"id": call.call_id, "name": call.name, **tool_payload},
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "name": call.name,
                            "content": json.dumps(tool_payload, ensure_ascii=False),
                        }
                    )

            raise ReferenceRuntimeError("reference runtime exceeded max_steps")
        except ReferenceRuntimeError as exc:
            partial_usage = dict(usage)
            self._merge_usage(partial_usage, exc.usage)
            raise ReferenceRuntimeError(
                str(exc),
                usage=partial_usage,
                trace=trace or exc.trace,
                duration_seconds=time.monotonic() - started,
            ) from exc

    @staticmethod
    def _validated_reply(reply: Any) -> ModelReply:
        if not isinstance(reply, ModelReply) or not isinstance(reply.content, str):
            raise ReferenceRuntimeError("model returned an invalid reply")
        if isinstance(reply.tool_calls, (str, bytes)) or not isinstance(
            reply.tool_calls, Sequence
        ):
            raise ReferenceRuntimeError("model returned an invalid reply")

        calls = []
        for call in reply.tool_calls:
            if not isinstance(call, ToolCall):
                raise ReferenceRuntimeError("model returned an invalid reply")
            if not isinstance(call.name, str) or not call.name.strip():
                raise ReferenceRuntimeError("model returned an invalid reply")
            if (
                not isinstance(call.arguments, Mapping)
                or not isinstance(call.call_id, str)
                or not call.call_id
            ):
                raise ReferenceRuntimeError("model returned an invalid reply")
            calls.append(
                ToolCall(
                    name=call.name,
                    arguments=dict(call.arguments),
                    call_id=call.call_id,
                )
            )
        usage = _validated_usage(reply.usage, "model returned an invalid reply")
        return ModelReply(content=reply.content, tool_calls=tuple(calls), usage=usage)

    @staticmethod
    def tool_specs() -> Sequence[Mapping[str, Any]]:
        return (
            {
                "name": "list_files",
                "description": "List regular files under a workspace-relative directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            {
                "name": "read_file",
                "description": "Read a UTF-8 workspace file.",
                "parameters": {
                    "type": "object",
                    "required": ["path"],
                    "properties": {"path": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            {
                "name": "write_file",
                "description": "Write a UTF-8 file under the workspace.",
                "parameters": {
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        )

    def _execute_tool(self, call: ToolCall, root: Path) -> Any:
        if call.name == "list_files":
            target = self._safe_path(root, str(call.arguments.get("path", ".")))
            if not target.is_dir():
                raise ValueError("list_files target is not a directory")
            return sorted(
                str(path.relative_to(root))
                for path in target.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
        if call.name == "read_file":
            target = self._safe_path(root, self._required_string(call, "path"))
            if not target.is_file() or target.is_symlink():
                raise ValueError("read_file target is not a regular file")
            payload = target.read_bytes()
            if len(payload) > self._config.max_read_bytes:
                raise ValueError("read_file exceeds byte limit")
            return payload.decode("utf-8")
        if call.name == "write_file":
            target = self._safe_path(root, self._required_string(call, "path"))
            content = self._required_string(call, "content")
            encoded = content.encode("utf-8")
            if len(encoded) > self._config.max_write_bytes:
                raise ValueError("write_file exceeds byte limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded)
            return {"path": str(target.relative_to(root)), "bytes": len(encoded)}
        raise ValueError("unknown tool: %s" % call.name)

    @staticmethod
    def _resolve_skill_file(skill_path: Path) -> Path:
        path = skill_path.resolve()
        if path.is_dir():
            path = path / "SKILL.md"
        if not path.is_file():
            raise ReferenceRuntimeError("SKILL.md not found: %s" % path)
        return path

    @staticmethod
    def _safe_path(root: Path, value: str) -> Path:
        relative = Path(value)
        if relative.is_absolute():
            raise ValueError("absolute paths are not allowed")
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("path escapes workspace") from exc
        return target

    @staticmethod
    def _required_string(call: ToolCall, key: str) -> str:
        value = call.arguments.get(key)
        if not isinstance(value, str):
            raise ValueError("%s must be a string" % key)
        return value

    def _append_trace(
        self,
        trace: List[Mapping[str, Any]],
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        if len(trace) >= self._config.max_trace_events:
            raise ReferenceRuntimeError("trace event limit exceeded")
        trace.append(
            {
                "seq": len(trace),
                "type": event_type,
                "timestamp": time.time(),
                "payload": dict(payload),
            }
        )

    @staticmethod
    def _merge_usage(target: Dict[str, Any], update: Mapping[str, Any]) -> None:
        for key, value in update.items():
            if isinstance(value, (int, float)) and isinstance(target.get(key, 0), (int, float)):
                target[key] = target.get(key, 0) + value
            else:
                target[key] = value
