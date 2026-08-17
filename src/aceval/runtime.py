"""Deterministic runtimes used by the reference EvalOps kernel.

The runtime boundary deliberately owns only process/session execution.  Fixture
preparation and artifact collection belong to :mod:`aceval.drivers`; graders
never run inside a runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union

from .agent_runtime import ReferenceAgentRuntime, ReferenceRuntimeError
from .contracts import (
    RuntimeCapabilities,
    RuntimeProfile,
    RuntimeResult,
    TraceEvent,
    as_primitive,
)


ScriptValue = Union[
    RuntimeResult,
    Mapping[str, Any],
    Callable[..., Union[RuntimeResult, Mapping[str, Any], Awaitable[Any]]],
]


class RuntimeConfigurationError(ValueError):
    """Raised before execution when a runtime profile is unsafe or incomplete."""


class _OutputLimitExceeded(Exception):
    pass


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _metadata(value: Any) -> Mapping[str, Any]:
    metadata = _get(value, "metadata", {})
    return metadata if isinstance(metadata, Mapping) else {}


def _subject_variant(subject: Any) -> Optional[str]:
    metadata = _metadata(subject)
    for key in ("variant", "version", "id", "name"):
        candidate = metadata.get(key)
        if candidate is None:
            candidate = _get(subject, key)
        if candidate is not None and str(candidate):
            text = str(candidate)
            if text.endswith("-candidate") or text == "candidate":
                return "candidate"
            if text.endswith("-baseline") or text == "baseline":
                return "baseline"
            return text
    path = _get(subject, "path") or _get(subject, "source_path")
    return Path(str(path)).name if path else None


def _scenario_id(prepared: Any) -> Optional[str]:
    metadata = _metadata(prepared)
    value = metadata.get("scenario_id") or metadata.get("id")
    return str(value) if value is not None else None


def _safe_workspace_path(workspace: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not relative or "\x00" in relative:
        raise RuntimeConfigurationError("artifact paths must be non-empty relative paths")
    target = workspace.joinpath(path)
    try:
        target.resolve(strict=False).relative_to(workspace.resolve())
    except ValueError as exc:
        raise RuntimeConfigurationError("artifact path escapes the scenario workspace") from exc
    current = workspace
    for part in path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise RuntimeConfigurationError("artifact path traverses a symlink")
    if target.is_symlink():
        raise RuntimeConfigurationError("artifact target is a symlink")
    return target


def _artifact_bytes(value: Any) -> bytes:
    if isinstance(value, Mapping) and "content" in value:
        value = value["content"]
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(
        as_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _write_declared_artifacts(workspace: Optional[str], artifacts: Mapping[str, Any]) -> None:
    if not artifacts:
        return
    if not workspace:
        raise RuntimeConfigurationError("declarative artifacts require a prepared workspace")
    root = Path(workspace)
    for relative, value in artifacts.items():
        target = _safe_workspace_path(root, str(relative))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_artifact_bytes(value))


def _trace_event(value: Any) -> TraceEvent:
    if isinstance(value, TraceEvent):
        return value
    if isinstance(value, str):
        return TraceEvent(kind="event", name=value)
    if not isinstance(value, Mapping):
        return TraceEvent(kind="event", payload={"value": value})
    known = {
        "kind",
        "type",
        "name",
        "payload",
        "timestamp",
        "seq",
        "tool",
        "duration_ms",
        "error",
    }
    payload = value.get("payload", {})
    if not isinstance(payload, Mapping):
        payload = {"value": payload}
    extras = {key: item for key, item in value.items() if key not in known}
    if extras:
        payload = dict(payload)
        payload.update(extras)
    return TraceEvent(
        kind=str(value.get("kind") or value.get("type") or "event"),
        seq=int(value.get("seq", 0)),
        name=str(value.get("name", "")),
        payload=payload,
        timestamp=(
            str(value["timestamp"])
            if value.get("timestamp") is not None
            else None
        ),
        tool=str(value["tool"]) if value.get("tool") is not None else None,
        duration_ms=(
            float(value["duration_ms"])
            if value.get("duration_ms") is not None
            else None
        ),
        error=str(value["error"]) if value.get("error") is not None else None,
    )


def _runtime_result(value: Any, workspace: Optional[str] = None) -> RuntimeResult:
    if isinstance(value, RuntimeResult):
        _write_declared_artifacts(workspace, value.artifacts)
        return RuntimeResult(
            final_output=as_primitive(value.final_output),
            trace=value.trace,
            error=value.error,
            usage=dict(value.usage),
            artifacts=dict(value.artifacts),
            metadata=dict(value.metadata),
        )
    if not isinstance(value, Mapping):
        value = {"final_output": value}
    else:
        value = as_primitive(value)
    artifacts = value.get("artifacts", {})
    if not isinstance(artifacts, Mapping):
        raise RuntimeConfigurationError("runtime artifacts must be a path-to-content mapping")
    _write_declared_artifacts(workspace, artifacts)
    trace_values = value.get("trace", ())
    if isinstance(trace_values, (str, bytes, Mapping)):
        trace_values = (trace_values,)
    trace = tuple(_trace_event(event) for event in trace_values)
    metadata = value.get("metadata", {})
    usage = value.get("usage", {})
    return RuntimeResult(
        final_output=value.get("final_output", value.get("output")),
        trace=trace,
        error=value.get("error"),
        usage=usage if isinstance(usage, Mapping) else {"value": usage},
        artifacts=dict(artifacts),
        metadata=metadata if isinstance(metadata, Mapping) else {"value": metadata},
    )


def _declarative_behavior(prepared: Any, subject: Any) -> Optional[Mapping[str, Any]]:
    metadata = _metadata(prepared)
    behavior = metadata.get("reference_runtime") or metadata.get("fake_runtime")
    if not isinstance(behavior, Mapping):
        return None
    variants = behavior.get("variants")
    if isinstance(variants, Mapping):
        variant = _subject_variant(subject)
        selected = variants.get(variant) if variant is not None else None
        if selected is None:
            selected = variants.get("default", behavior.get("default"))
        if selected is None:
            return {
                "error": "no declarative runtime behavior for subject variant: %s"
                % (variant or "<unknown>"),
            }
        if not isinstance(selected, Mapping):
            return {"final_output": selected}
        merged = {
            key: value
            for key, value in behavior.items()
            if key not in ("variants", "default")
        }
        merged.update(selected)
        return merged
    return behavior


class FakeRuntime:
    """A zero-model runtime for conformance, replay, and state-machine tests.

    Scripts may be keyed by ``(scenario_id, subject_variant)``, scenario id,
    subject variant, or ``"default"``.  With no explicit script the runtime
    reads ``prepared.metadata.fake_runtime``.  The latter supports a
    ``variants`` mapping so baseline and candidate snapshots can share a case.
    """

    id = "fake"

    def __init__(self, scripts: Optional[Mapping[Any, ScriptValue]] = None) -> None:
        self.scripts: Dict[Any, ScriptValue] = dict(scripts or {})
        self.calls = []  # type: list

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities.from_values(
            {
                "headless",
                "fresh_session",
                "workspace_fixture",
                "artifact_output",
                "canonical_trace",
                "fixed_parameters",
                "skill_activation",
            }
        )

    @property
    def profile(self) -> RuntimeProfile:
        return RuntimeProfile(
            adapter=self.id,
            capabilities=self.capabilities,
            parameters={"script_count": len(self.scripts)},
            metadata={
                "profile_complete": True,
                "protocol_version": "aceval.runtime/v1",
                "simulated": True,
            },
        )

    def _select_script(self, subject: Any, prepared: Any) -> Optional[ScriptValue]:
        scenario_id = _scenario_id(prepared)
        variant = _subject_variant(subject)
        for key in ((scenario_id, variant), scenario_id, variant, "default"):
            if key in self.scripts:
                return self.scripts[key]
        return _declarative_behavior(prepared, subject)

    async def execute(
        self,
        prepared: Any,
        subject: Any,
        context: Any = None,
    ) -> RuntimeResult:
        profile = _get(context, "runtime_profile")
        budget = _get(context, "budget")
        self.calls.append(
            {
                "scenario_id": _scenario_id(prepared),
                "subject_variant": _subject_variant(subject),
                "profile": profile,
                "budget": budget,
            }
        )
        script = self._select_script(subject, prepared)
        if script is None:
            return RuntimeResult(error="no fake runtime behavior configured")
        if callable(script):
            try:
                value = script(prepared, subject, context)
            except TypeError:
                value = script(subject, prepared)
            if inspect.isawaitable(value):
                value = await value
        else:
            value = script
        return _runtime_result(value, _get(prepared, "workspace"))

    async def cancel(self, run_id: str) -> bool:
        # Fake execution is atomic; cancellation is intentionally idempotent.
        return False


class SubprocessRuntime:
    """A local, operator-controlled reference runtime.

    It runs one allowlisted subprocess.  Commands are argv sequences and are
    never evaluated by a shell.  This is process hygiene, not a hostile-code
    sandbox; only trusted commands should be allowlisted by the operator.
    """

    id = "subprocess"

    def __init__(
        self,
        allowed_commands: Optional[Iterable[str]] = None,
        default_command: Optional[Sequence[str]] = None,
        environment_allowlist: Iterable[str] = ("PATH", "LANG", "LC_ALL", "TZ"),
        default_timeout_seconds: float = 60.0,
        default_max_output_bytes: int = 1024 * 1024,
    ) -> None:
        commands = tuple(allowed_commands or (sys.executable,))
        self._allowed_commands = frozenset(self._canonical_executable(item) for item in commands)
        self._default_command = tuple(default_command) if default_command else None
        self._environment_allowlist = frozenset(environment_allowlist)
        self._default_timeout = float(default_timeout_seconds)
        self._default_max_output = int(default_max_output_bytes)
        if self._default_timeout <= 0 or self._default_max_output <= 0:
            raise ValueError("runtime limits must be positive")
        self._processes = {}  # type: Dict[str, asyncio.subprocess.Process]

    @staticmethod
    def _canonical_executable(executable: str) -> str:
        path = shutil.which(str(executable))
        if path is None:
            path = str(Path(str(executable)).expanduser())
        return str(Path(path).resolve())

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities.from_values(
            {
                "headless",
                "fresh_session",
                "workspace_fixture",
                "artifact_output",
                "canonical_trace",
                "subprocess",
                "timeout",
                "cancel",
                "fixed_parameters",
                "skill_activation",
            }
        )

    @property
    def profile(self) -> RuntimeProfile:
        return RuntimeProfile(
            adapter=self.id,
            capabilities=self.capabilities,
            parameters={
                "allowed_commands": tuple(sorted(self._allowed_commands)),
                "default_command": self._default_command,
                "default_timeout_seconds": self._default_timeout,
                "default_max_output_bytes": self._default_max_output,
                "environment_allowlist": tuple(
                    sorted(self._environment_allowlist)
                ),
            },
            metadata={
                "profile_complete": True,
                "protocol_version": "aceval.runtime/v1",
            },
        )

    def _parameters(self, profile: Any) -> Mapping[str, Any]:
        parameters = _get(profile, "parameters", {})
        return parameters if isinstance(parameters, Mapping) else {}

    def _command(self, profile: Any) -> Tuple[str, ...]:
        parameters = self._parameters(profile)
        command = _get(profile, "command") or parameters.get("command") or self._default_command
        if isinstance(command, str):
            raise RuntimeConfigurationError("command must be an argv sequence, not a shell string")
        if not isinstance(command, Sequence) or not command:
            raise RuntimeConfigurationError("no command configured in the operator runtime profile")
        argv = tuple(str(item) for item in command)
        if any("\x00" in item for item in argv):
            raise RuntimeConfigurationError("command contains a NUL byte")
        executable = self._canonical_executable(argv[0])
        if executable not in self._allowed_commands:
            raise RuntimeConfigurationError("executable is not in the runtime allowlist")
        return (executable,) + argv[1:]

    def _limits(self, profile: Any, budget: Any) -> Tuple[float, int]:
        parameters = self._parameters(profile)
        timeout = (
            _get(budget, "max_wall_time_seconds")
            or parameters.get("timeout_seconds")
            or self._default_timeout
        )
        output_limit = (
            _get(budget, "max_output_bytes")
            or parameters.get("max_output_bytes")
            or self._default_max_output
        )
        try:
            timeout_value = float(timeout)
            output_value = int(output_limit)
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigurationError("runtime limits must be numeric") from exc
        if timeout_value <= 0 or output_value <= 0:
            raise RuntimeConfigurationError("runtime limits must be positive")
        return timeout_value, output_value

    def _environment(self, profile: Any, workspace: Path, subject: Any) -> Dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in self._environment_allowlist
        }
        parameters = self._parameters(profile)
        requested = _get(profile, "environment") or parameters.get("environment", {})
        if requested:
            if not isinstance(requested, Mapping):
                raise RuntimeConfigurationError("profile environment must be a mapping")
            forbidden = set(requested) - self._environment_allowlist
            if forbidden:
                raise RuntimeConfigurationError(
                    "profile requested non-allowlisted environment variables: %s"
                    % ", ".join(sorted(str(item) for item in forbidden))
                )
            environment.update({str(key): str(value) for key, value in requested.items()})
        environment["HOME"] = str(workspace)
        environment["PYTHONIOENCODING"] = "utf-8"
        subject_metadata = _metadata(subject)
        subject_path = (
            subject_metadata.get("materialized_path")
            or subject_metadata.get("path")
            or _get(subject, "uri")
        )
        if subject_path:
            environment["ACEVAL_SUBJECT_PATH"] = str(subject_path)
        return environment

    async def _read_limited(self, stream: Any, limit: int) -> bytes:
        chunks = []
        size = 0
        while True:
            chunk = await stream.read(min(65536, limit + 1))
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > limit:
                raise _OutputLimitExceeded
            chunks.append(chunk)

    async def _communicate(
        self, process: asyncio.subprocess.Process, prompt: bytes, output_limit: int
    ) -> Tuple[bytes, bytes]:
        async def write_stdin() -> None:
            if process.stdin is None:
                return
            try:
                process.stdin.write(prompt)
                await process.stdin.drain()
                process.stdin.close()
                if hasattr(process.stdin, "wait_closed"):
                    await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

        writer = asyncio.create_task(write_stdin())
        stdout_reader = asyncio.create_task(self._read_limited(process.stdout, output_limit))
        stderr_reader = asyncio.create_task(self._read_limited(process.stderr, output_limit))
        try:
            stdout, stderr = await asyncio.gather(stdout_reader, stderr_reader)
            await writer
            await process.wait()
            return stdout, stderr
        finally:
            for task in (writer, stdout_reader, stderr_reader):
                if not task.done():
                    task.cancel()

    async def execute(
        self,
        prepared: Any,
        subject: Any,
        context: Any = None,
    ) -> RuntimeResult:
        profile = _get(context, "runtime_profile")
        budget = _get(context, "budget")
        workspace_value = _get(prepared, "workspace")
        if not workspace_value:
            return RuntimeResult(error="reference runtime requires a prepared workspace")
        workspace = Path(str(workspace_value)).resolve()
        if not workspace.is_dir():
            return RuntimeResult(error="prepared workspace does not exist")

        try:
            argv = self._command(profile)
            timeout, output_limit = self._limits(profile, budget)
            environment = self._environment(profile, workspace, subject)
        except RuntimeConfigurationError as exc:
            return RuntimeResult(error="configuration_error: %s" % exc)

        prompt = str(_get(prepared, "prompt", "")).encode("utf-8")
        run_id = str(_metadata(prepared).get("run_id") or id(asyncio.current_task()))
        started = time.monotonic()
        trace = [
            TraceEvent(
                kind="runtime",
                name="process_started",
                payload={"argv": list(argv), "cwd": str(workspace), "run_id": run_id},
                timestamp=time.time(),
            )
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workspace),
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            return RuntimeResult(
                error="spawn_error: %s" % exc,
                trace=tuple(trace),
                usage={"wall_time_seconds": time.monotonic() - started},
            )

        self._processes[run_id] = process
        error = None
        stdout = b""
        stderr = b""
        try:
            stdout, stderr = await asyncio.wait_for(
                self._communicate(process, prompt, output_limit), timeout=timeout
            )
        except asyncio.TimeoutError:
            error = "timeout"
            process.kill()
            await process.wait()
        except _OutputLimitExceeded:
            error = "output_limit_exceeded"
            process.kill()
            await process.wait()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        finally:
            self._processes.pop(run_id, None)

        if error is None and process.returncode:
            error = "process_exit:%s" % process.returncode
        final_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        parameters = self._parameters(profile)
        final_output = final_text
        if parameters.get("parse_json"):
            try:
                final_output = json.loads(final_text)
            except json.JSONDecodeError:
                error = error or "invalid_json_output"
        trace.append(
            TraceEvent(
                kind="message",
                name="final_output",
                payload={"content": final_text},
                timestamp=time.time(),
            )
        )
        trace.append(
            TraceEvent(
                kind="runtime",
                name="process_finished",
                payload={
                    "run_id": run_id,
                    "exit_code": process.returncode,
                    "error": error,
                },
                timestamp=time.time(),
            )
        )
        return RuntimeResult(
            final_output=final_output,
            trace=tuple(trace),
            error=error,
            usage={
                "wall_time_seconds": time.monotonic() - started,
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
                "exit_code": process.returncode,
            },
            metadata={"stderr": stderr_text, "run_id": run_id, "argv": list(argv)},
        )

    async def cancel(self, run_id: str) -> bool:
        process = self._processes.get(str(run_id))
        if process is None or process.returncode is not None:
            return False
        process.kill()
        await process.wait()
        return True


class ReferenceRuntimeAdapter:
    """Expose :class:`ReferenceAgentRuntime` through the Kernel contract."""

    id = "reference"

    def __init__(self, engine: ReferenceAgentRuntime) -> None:
        self._engine = engine

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities.from_values(
            {
                "headless",
                "fresh_session",
                "workspace_fixture",
                "artifact_output",
                "canonical_trace",
                "fixed_parameters",
                "skill_activation",
            }
        )

    @property
    def profile(self) -> RuntimeProfile:
        engine_profile = getattr(self._engine, "profile", None)
        if callable(engine_profile):
            engine_profile = engine_profile()
        profile_mapping = (
            dict(engine_profile) if isinstance(engine_profile, Mapping) else {}
        )
        bridge = profile_mapping.get("model_bridge")
        bridge_mapping = dict(bridge) if isinstance(bridge, Mapping) else {}
        tool_specs = as_primitive(ReferenceAgentRuntime.tool_specs())
        tool_payload = json.dumps(
            tool_specs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return RuntimeProfile(
            adapter=self.id,
            model=(
                str(bridge_mapping["model_id"])
                if bridge_mapping.get("model_id")
                else None
            ),
            capabilities=self.capabilities,
            parameters=profile_mapping,
            tool_policy={
                "tool_names": tuple(item["name"] for item in tool_specs),
                "tool_specs_sha256": hashlib.sha256(tool_payload).hexdigest(),
            },
            environment_hash=bridge_mapping.get("environment_presence_hash"),
            metadata={
                # Bridge v1 cannot attest provider-observed model parameters.
                "profile_complete": False,
                "protocol_version": "aceval.runtime/v1",
                "inference_profile_scope": "operator_declared_only",
                "model_id_source": (
                    "operator_declared"
                    if bridge_mapping.get("model_id")
                    else "not_declared"
                ),
            },
        )

    @staticmethod
    def _skill_path(subject: Any) -> Path:
        metadata = _metadata(subject)
        value = metadata.get("materialized_path") or metadata.get("path") or _get(subject, "uri")
        if not value:
            raise RuntimeConfigurationError("subject has no materialized Skill path")
        text_value = str(value)
        if text_value.startswith("file://"):
            text_value = text_value[7:]
        path = Path(text_value).expanduser().resolve()
        if not path.exists():
            raise RuntimeConfigurationError("materialized Skill path does not exist")
        return path

    @staticmethod
    def _canonical_trace(events: Iterable[Mapping[str, Any]]) -> Tuple[TraceEvent, ...]:
        result = []
        for index, event in enumerate(events):
            payload = event.get("payload", {})
            if not isinstance(payload, Mapping):
                payload = {"value": payload}
            kind = str(event.get("kind") or event.get("type") or "event")
            tool = event.get("tool")
            if tool is None and kind in ("tool_call", "tool_result"):
                tool = payload.get("name")
            result.append(
                TraceEvent(
                    kind=kind,
                    seq=int(event.get("seq", index)),
                    timestamp=str(event["timestamp"]) if event.get("timestamp") is not None else None,
                    name=str(event.get("name") or payload.get("name") or ""),
                    payload=dict(payload),
                    tool=str(tool) if tool is not None else None,
                    duration_ms=event.get("duration_ms"),
                    error=event.get("error"),
                )
            )
        return tuple(result)

    async def execute(self, prepared: Any, subject: Any, context: Any = None) -> RuntimeResult:
        if not _get(prepared, "workspace"):
            return RuntimeResult(error="reference runtime requires a prepared workspace")
        try:
            skill_path = self._skill_path(subject)
        except RuntimeConfigurationError as exc:
            return RuntimeResult(error="configuration_error: %s" % exc)

        loop = asyncio.get_running_loop()
        run_kwargs = {
            "skill_path": skill_path,
            "prompt": str(_get(prepared, "prompt", "")),
            "workspace": Path(_get(prepared, "workspace")),
        }
        if isinstance(self._engine, ReferenceAgentRuntime):
            remaining_tool_calls = _get(_get(context, "budget"), "max_tool_calls")
            if remaining_tool_calls is not None:
                run_kwargs["max_tool_calls"] = int(remaining_tool_calls)
        call = lambda: self._engine.run(**run_kwargs)
        future = loop.run_in_executor(None, call)
        timeout = _get(_get(context, "budget"), "max_wall_time_seconds")
        try:
            if timeout:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=float(timeout))
            else:
                result = await future
        except asyncio.TimeoutError:
            # This budget is a soft deadline, not a strict wall-clock cutoff.
            # CommandModelClient owns the terminating bridge timeout, and we
            # wait for that controlled call so Driver.collect/cleanup never
            # races a worker that can still mutate the scenario workspace.
            partial_usage = {}
            partial_trace = ()
            try:
                completed = await future
                partial_usage = dict(completed.usage)
                partial_usage.setdefault(
                    "wall_time_seconds", completed.duration_seconds
                )
                partial_trace = self._canonical_trace(completed.trace)
            except ReferenceRuntimeError as exc:
                partial_usage = dict(exc.usage)
                partial_usage.setdefault("wall_time_seconds", exc.duration_seconds)
                partial_trace = self._canonical_trace(exc.trace)
            except Exception:
                # The budget outcome stays ``timeout`` even if the bridge
                # reports its own terminal failure while it is winding down.
                pass
            return RuntimeResult(
                error="timeout", usage=partial_usage, trace=partial_trace
            )
        except (ReferenceRuntimeError, OSError, ValueError) as exc:
            usage = dict(getattr(exc, "usage", {}) or {})
            duration = getattr(exc, "duration_seconds", None)
            if duration is not None:
                usage.setdefault("wall_time_seconds", duration)
            trace = self._canonical_trace(getattr(exc, "trace", ()) or ())
            return RuntimeResult(
                error="runtime_error: %s" % exc,
                usage=usage,
                trace=trace,
            )

        usage = dict(result.usage)
        usage.setdefault("wall_time_seconds", result.duration_seconds)
        return RuntimeResult(
            final_output=result.final_output,
            trace=self._canonical_trace(result.trace),
            usage=usage,
            metadata={"runtime": "reference_agent"},
        )

    async def cancel(self, run_id: str) -> bool:
        del run_id
        return False


ReferenceRuntime = ReferenceRuntimeAdapter
LocalReferenceRuntime = SubprocessRuntime


__all__ = [
    "FakeRuntime",
    "LocalReferenceRuntime",
    "ReferenceRuntime",
    "ReferenceRuntimeAdapter",
    "RuntimeConfigurationError",
    "SubprocessRuntime",
]
