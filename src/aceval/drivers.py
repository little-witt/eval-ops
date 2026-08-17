"""Built-in scenario workspace drivers.

Drivers own the temporary workspace lifecycle.  They copy only declared
fixtures, freeze file/artifact observations before cleanup, and never inspect
an oracle or assign a grade.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Sequence, Set, Tuple

from .contracts import (
    Artifact,
    PreparedScenario,
    RunObservation,
    RuntimeResult,
    as_primitive,
)


class DriverError(ValueError):
    """A scenario cannot be prepared or collected without violating isolation."""


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _scenario(value: Any) -> Any:
    return _get(value, "scenario", value)


def _metadata(value: Any) -> Mapping[str, Any]:
    metadata = _get(value, "metadata", {})
    return metadata if isinstance(metadata, Mapping) else {}


def _safe_relative(value: Any) -> Path:
    text = str(value)
    path = Path(text)
    if text in ("", ".") or "\x00" in text or path.is_absolute() or ".." in path.parts:
        raise DriverError("workspace paths must be non-empty relative paths")
    return path


def _inside(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DriverError("path escapes its declared root") from exc
    return candidate


def _reject_symlink_path(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DriverError("symlinks are not allowed in fixture or artifact paths")


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise DriverError("fixture contains a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.is_dir():
        raise DriverError("fixture target collides with a directory")
    shutil.copyfile(str(source), str(destination), follow_symlinks=False)


def _copy_directory_contents(source: Path, destination: Path) -> None:
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if item.is_symlink():
            raise DriverError("fixture contains a symlink: %s" % relative)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            _copy_file(item, target)
        else:
            raise DriverError("fixture contains a non-regular file: %s" % relative)


def _file_content(path: Path, maximum: int) -> bytes:
    size = path.stat().st_size
    if size > maximum:
        raise DriverError("observed file exceeds per-file byte limit: %s" % path.name)
    return path.read_bytes()


def _artifact(path: str, content: bytes, producer: str = "workspace") -> Artifact:
    mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Artifact(
        path=path,
        content_hash=hashlib.sha256(content).hexdigest(),
        mime_type=mime_type,
        size=len(content),
        content=content,
        producer=producer,
    )


class _WorkspaceDriver:
    id = "workspace"
    capability_values = frozenset(
        {"fresh_session", "workspace_fixture", "canonical_trace"}
    )

    def __init__(
        self,
        max_files: int = 5000,
        max_file_bytes: int = 4 * 1024 * 1024,
        max_total_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        if min(max_files, max_file_bytes, max_total_bytes) <= 0:
            raise ValueError("driver observation limits must be positive")
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes
        self._owned = set()  # type: Set[Path]

    def required_capabilities(self, scenario: Any) -> FrozenSet[str]:
        del scenario
        return self.capability_values

    def _pack_root(self, context: Any) -> Path:
        metadata = _metadata(context)
        value = metadata.get("pack_root") or metadata.get("fixture_root")
        if value is None:
            raise DriverError("RunContext.metadata.pack_root is required for fixtures")
        root = Path(str(value)).expanduser().resolve()
        if not root.is_dir():
            raise DriverError("pack_root does not exist or is not a directory")
        return root

    def _work_root(self, context: Any) -> Optional[Path]:
        metadata = _metadata(context)
        value = metadata.get("work_root") or metadata.get("workspace_root")
        if value is None:
            return None
        root = Path(str(value)).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _copy_fixtures(self, fixtures: Sequence[Any], pack_root: Path, workspace: Path) -> None:
        for fixture in fixtures:
            if isinstance(fixture, Mapping):
                source_value = fixture.get("source") or fixture.get("path")
                target_value = fixture.get("target", ".")
            else:
                source_value = fixture
                target_value = "."
            source_relative = _safe_relative(source_value)
            target_relative = Path(".") if str(target_value) == "." else _safe_relative(target_value)
            _reject_symlink_path(pack_root, source_relative)
            source = _inside(pack_root, pack_root / source_relative)
            target = _inside(workspace, workspace / target_relative)
            if not source.exists():
                raise DriverError("fixture does not exist: %s" % source_relative)
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                _copy_directory_contents(source, target)
            elif source.is_file():
                if str(target_value) == ".":
                    target = workspace / source.name
                _copy_file(source, target)
            else:
                raise DriverError("fixture must be a regular file or directory")

    def _snapshot(self, workspace: Path) -> Mapping[str, Artifact]:
        files = {}  # type: Dict[str, Artifact]
        total = 0
        for path in sorted(workspace.rglob("*")):
            relative = path.relative_to(workspace)
            if path.is_symlink():
                raise DriverError("workspace contains a symlink: %s" % relative)
            if path.is_dir():
                continue
            if not path.is_file():
                raise DriverError("workspace contains a non-regular file: %s" % relative)
            if len(files) >= self._max_files:
                raise DriverError("workspace exceeds file-count limit")
            content = _file_content(path, self._max_file_bytes)
            total += len(content)
            if total > self._max_total_bytes:
                raise DriverError("workspace exceeds total byte limit")
            key = relative.as_posix()
            files[key] = _artifact(key, content)
        return files

    def _artifact_paths(self, scenario: Any, context: Any) -> Tuple[str, ...]:
        raw = []  # type: list
        scenario_metadata = _metadata(_scenario(scenario))
        context_metadata = _metadata(context)
        for value in (
            scenario_metadata.get("artifact_paths", ()),
            context_metadata.get("artifact_paths", ()),
        ):
            if isinstance(value, str):
                raw.append(value)
            elif isinstance(value, Iterable):
                raw.extend(value)

        grader_params = _get(_scenario(scenario), "grader_params", {})
        if isinstance(grader_params, Mapping):
            for config in grader_params.values():
                if not isinstance(config, Mapping):
                    continue
                if config.get("artifact_path"):
                    raw.append(config["artifact_path"])
                if config.get("path") and config.get("artifact", False):
                    raw.append(config["path"])
                paths = config.get("paths", ()) if config.get("artifact", False) else ()
                raw.extend((paths,) if isinstance(paths, str) else paths)
        normalized = []
        for value in raw:
            text = _safe_relative(value).as_posix()
            if text not in normalized:
                normalized.append(text)
        return tuple(normalized)

    async def prepare(self, scenario: Any, context: Any) -> PreparedScenario:
        case = _scenario(scenario)
        fixtures = tuple(_get(case, "fixtures", ()) or ())
        pack_root = self._pack_root(context) if fixtures else None
        work_root = self._work_root(context)
        workspace = Path(
            tempfile.mkdtemp(
                prefix="aceval-%s-" % self.id.replace("_", "-"),
                dir=str(work_root) if work_root else None,
            )
        ).resolve()
        self._owned.add(workspace)
        try:
            if fixtures and pack_root is not None:
                self._copy_fixtures(fixtures, pack_root, workspace)
            baseline = self._snapshot(workspace)
            scenario_metadata = dict(_metadata(case))
            metadata = {
                "driver_id": self.id,
                "scenario_id": str(_get(case, "id", "")),
                "run_id": str(_get(context, "run_id", "")),
                "workspace_owned": True,
            }
            # Only runtime behavior is forwarded.  Oracle refs and grader data
            # are deliberately absent from the prepared scenario.
            for key in ("fake_runtime", "reference_runtime"):
                if key in scenario_metadata:
                    metadata[key] = scenario_metadata[key]
            return PreparedScenario(
                workspace=workspace,
                prompt=str(_get(case, "prompt", "")),
                baseline_files=baseline,
                artifact_paths=self._artifact_paths(case, context),
                metadata=metadata,
            )
        except Exception:
            await self._cleanup_path(workspace)
            raise

    def _freeze_runtime_artifact(
        self, relative: str, value: Any, workspace: Path
    ) -> Artifact:
        if isinstance(value, Artifact):
            content = value.content
            if isinstance(content, str):
                content = content.encode("utf-8")
            elif content is not None and not isinstance(content, bytes):
                content = json.dumps(
                    as_primitive(content),
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                ).encode("utf-8")
            if content is None:
                relative_path = _safe_relative(relative)
                _reject_symlink_path(workspace, relative_path)
                path = _inside(workspace, workspace / relative_path)
                if not path.is_file() or path.is_symlink():
                    raise DriverError(
                        "contentless runtime artifact is not backed by a workspace file"
                    )
                content = _file_content(path, self._max_file_bytes)
                return _artifact(relative, content, producer="workspace")
            if len(content) > self._max_file_bytes:
                raise DriverError("runtime artifact exceeds per-file byte limit")
            return Artifact(
                path=relative,
                content_hash=hashlib.sha256(content).hexdigest(),
                mime_type=value.mime_type,
                size=len(content),
                content=content,
                producer=value.producer or "runtime",
                metadata=value.metadata,
            )
        if isinstance(value, Mapping) and "content" in value:
            content = value["content"]
        else:
            content = value
        if isinstance(content, str):
            payload = content.encode("utf-8")
        elif isinstance(content, bytes):
            payload = content
        else:
            payload = json.dumps(
                as_primitive(content),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        if len(payload) > self._max_file_bytes:
            raise DriverError("runtime artifact exceeds per-file byte limit")
        return _artifact(relative, payload, producer="runtime")

    async def collect(self, prepared: PreparedScenario, result: RuntimeResult) -> RunObservation:
        if prepared.workspace is None:
            raise DriverError("prepared scenario has no workspace")
        workspace = Path(prepared.workspace).resolve()
        if workspace not in self._owned or not workspace.is_dir():
            raise DriverError("prepared workspace is not owned by this driver")
        post_state = self._snapshot(workspace)
        artifacts = {}  # type: Dict[str, Artifact]
        artifact_bytes = 0

        def add_artifact(relative: str, artifact: Artifact) -> None:
            nonlocal artifact_bytes
            if relative in artifacts:
                return
            if len(artifacts) >= self._max_files:
                raise DriverError("runtime artifacts exceed file-count limit")
            content = artifact.content
            if not isinstance(content, bytes):
                raise DriverError("normalized runtime artifact must contain bytes")
            size = len(content)
            if size > self._max_file_bytes:
                raise DriverError("runtime artifact exceeds per-file byte limit")
            if artifact_bytes + size > self._max_total_bytes:
                raise DriverError("runtime artifacts exceed total byte limit")
            artifact_bytes += size
            artifacts[relative] = artifact

        for value in prepared.artifact_paths:
            relative = _safe_relative(value)
            _reject_symlink_path(workspace, relative)
            path = _inside(workspace, workspace / relative)
            if path.is_file() and not path.is_symlink():
                content = _file_content(path, self._max_file_bytes)
                relative_value = relative.as_posix()
                add_artifact(relative_value, _artifact(relative_value, content))

        for path_value, value in result.artifacts.items():
            relative = _safe_relative(path_value).as_posix()
            add_artifact(
                relative,
                self._freeze_runtime_artifact(relative, value, workspace),
            )

        return RunObservation(
            output=result.final_output,
            trace=tuple(result.trace),
            artifacts=artifacts,
            pre_state=dict(prepared.baseline_files),
            post_state=post_state,
            workspace_root=None,
            error=result.error,
            usage=dict(result.usage),
            metadata={
                "driver_id": self.id,
                "scenario_id": _metadata(prepared).get("scenario_id"),
                "runtime_metadata": dict(result.metadata),
                "runtime_adapter_exception": bool(
                    result.metadata.get("runtime_adapter_exception", False)
                ),
                "partial_accounting": bool(
                    result.metadata.get("partial_accounting", False)
                ),
            },
        )

    async def _cleanup_path(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        if resolved not in self._owned:
            return
        self._owned.discard(resolved)
        if resolved.exists():
            shutil.rmtree(str(resolved))

    async def cleanup(self, prepared: PreparedScenario) -> None:
        if prepared.workspace is None:
            return
        await self._cleanup_path(Path(prepared.workspace))


class RepositoryWorkspaceDriver(_WorkspaceDriver):
    id = "repository_workspace"


class ArtifactWorkspaceDriver(_WorkspaceDriver):
    id = "artifact_workspace"
    capability_values = _WorkspaceDriver.capability_values.union({"artifact_output"})


def builtin_drivers() -> Mapping[str, _WorkspaceDriver]:
    """Return fresh built-in driver instances for a component registry."""

    return {
        RepositoryWorkspaceDriver.id: RepositoryWorkspaceDriver(),
        ArtifactWorkspaceDriver.id: ArtifactWorkspaceDriver(),
    }


def register_builtin_drivers(registry: Any) -> None:
    for component_id, driver in builtin_drivers().items():
        registry.register_driver(component_id, driver)


__all__ = [
    "ArtifactWorkspaceDriver",
    "DriverError",
    "RepositoryWorkspaceDriver",
    "builtin_drivers",
    "register_builtin_drivers",
]
