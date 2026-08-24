"""Local validation providers and lifecycle enforcement.

The Docker provider never invokes a shell, never pulls an image implicitly,
and binds a verified candidate directory read-only.  A provider error is kept
separate from a validation command failure so infrastructure cannot be used as
evidence that a Skill candidate is bad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import subprocess
import time
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple
import uuid

from .environment_contracts import (
    CommandReceipt,
    CommandSpec,
    EnvironmentBlueprint,
    EnvironmentContractError,
    VALIDATION_RECEIPT_API_VERSION,
    ValidationReceipt,
    ValidationRequest,
    ValidationStatus,
    canonical_hash,
    normalize_sha256,
    verify_candidate_bundle,
)


MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024


class EnvironmentProviderError(RuntimeError):
    """The provider could not safely create, inspect, or clean up an environment."""


class EnvironmentUnavailableError(EnvironmentProviderError):
    """The selected provider is not installed or healthy."""


@dataclass(frozen=True)
class ProcessResult:
    argv: Tuple[str, ...]
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    duration_ms: float = 0.0


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cwd: Optional[Path] = None,
    ) -> ProcessResult:
        ...


class SubprocessCommandRunner:
    """Run an explicit argv with a bounded output buffer and no shell."""

    def __init__(self, max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES) -> None:
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        self.max_output_bytes = max_output_bytes

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cwd: Optional[Path] = None,
    ) -> ProcessResult:
        values = tuple(str(item) for item in argv)
        if not values:
            raise EnvironmentProviderError("command argv must not be empty")
        started = time.monotonic()
        try:
            completed = subprocess.run(
                values,
                cwd=str(cwd) if cwd is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise EnvironmentUnavailableError("command is not installed: %s" % values[0]) from exc
        except subprocess.TimeoutExpired as exc:
            stdout = bytes(exc.stdout or b"")[: self.max_output_bytes]
            stderr = bytes(exc.stderr or b"")[: self.max_output_bytes]
            error = subprocess.TimeoutExpired(values, timeout_seconds, output=stdout, stderr=stderr)
            raise error from exc
        duration = (time.monotonic() - started) * 1000.0
        return ProcessResult(
            argv=values,
            returncode=completed.returncode,
            stdout=completed.stdout[: self.max_output_bytes],
            stderr=completed.stderr[: self.max_output_bytes],
            duration_ms=duration,
        )


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_output_path(root: Path, relative: str) -> Path:
    target = root.joinpath(relative)
    try:
        target.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise EnvironmentProviderError("output path escapes candidate artifact") from exc
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise EnvironmentProviderError("output path traverses a symlink")
    return target


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class CapabilityReport:
    provider: str
    ready: bool
    docker_version: Optional[str] = None
    image_id: Optional[str] = None
    missing: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()
    fingerprint: Optional[str] = None


@dataclass
class EnvironmentInstance:
    provider: str
    container_name: str
    candidate_root: Path
    image_id: str
    fingerprint: str
    blueprint: EnvironmentBlueprint
    cleaned: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


class LocalDockerProvider:
    id = "local-docker"

    def __init__(
        self,
        runner: Optional[CommandRunner] = None,
        *,
        docker_command: str = "docker",
    ) -> None:
        self.runner = runner or SubprocessCommandRunner()
        self.docker_command = docker_command

    def _run(self, argv: Sequence[str], timeout_seconds: float = 30.0) -> ProcessResult:
        return self.runner.run(argv, timeout_seconds=timeout_seconds)

    def _docker_version(self) -> str:
        result = self._run(
            (self.docker_command, "version", "--format", "{{.Server.Version}}"),
            15.0,
        )
        if result.returncode != 0:
            raise EnvironmentUnavailableError(
                "Docker daemon is unavailable: %s" % _decode(result.stderr).strip()
            )
        version = _decode(result.stdout).strip()
        if not version:
            raise EnvironmentUnavailableError("Docker daemon returned no version")
        return version

    def _image_id(self, image: str) -> str:
        result = self._run(
            (self.docker_command, "image", "inspect", image, "--format", "{{.Id}}"),
            30.0,
        )
        if result.returncode != 0:
            raise EnvironmentUnavailableError(
                "pinned Docker image is unavailable locally: %s" % image
            )
        image_id = _decode(result.stdout).strip()
        return normalize_sha256(image_id, "Docker image id")

    def preflight(self, blueprint: EnvironmentBlueprint) -> CapabilityReport:
        if blueprint.provider != self.id:
            return CapabilityReport(
                provider=self.id,
                ready=False,
                missing=("provider:%s" % blueprint.provider,),
                errors=("blueprint requires a different provider",),
            )
        try:
            version = self._docker_version()
            image_id = self._image_id(blueprint.image)
            # A repository@sha256 value pins a registry manifest. Docker's
            # local image Id is the config digest and is intentionally not the
            # same value. A raw sha256 image reference, however, is the Id.
            if (
                "@" not in blueprint.image
                and blueprint.image.startswith("sha256:")
                and normalize_sha256(blueprint.image) != image_id
            ):
                raise EnvironmentUnavailableError(
                    "Docker image id does not match the pinned blueprint digest"
                )
            fingerprint = canonical_hash(
                {
                    "provider": self.id,
                    "docker_version": version,
                    "image_id": image_id,
                    "blueprint_hash": blueprint.blueprint_hash,
                }
            )
            return CapabilityReport(
                provider=self.id,
                ready=True,
                docker_version=version,
                image_id=image_id,
                fingerprint=fingerprint,
            )
        except (EnvironmentProviderError, EnvironmentContractError) as exc:
            return CapabilityReport(
                provider=self.id,
                ready=False,
                missing=("docker", "pinned_image"),
                errors=(str(exc),),
            )

    def prepare(self, request: ValidationRequest) -> EnvironmentInstance:
        blueprint = request.blueprint
        report = self.preflight(blueprint)
        if not report.ready or report.image_id is None or report.fingerprint is None:
            raise EnvironmentUnavailableError("; ".join(report.errors) or "Docker preflight failed")
        candidate_root = verify_candidate_bundle(request.candidate)
        name = "aceval-%s" % uuid.uuid4().hex[:20]
        limits = blueprint.limits
        create_argv = (
            self.docker_command,
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--cpus",
            str(limits.cpus),
            "--memory",
            "%sm" % limits.memory_mb,
            "--pids-limit",
            str(limits.pids),
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=%sm" % limits.tmpfs_mb,
            "--mount",
            "type=bind,src=%s,dst=/workspace,readonly" % candidate_root,
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/tmp/aceval-home",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            blueprint.image,
            "tail",
            "-f",
            "/dev/null",
        )
        create = self._run(create_argv, 60.0)
        if create.returncode != 0:
            raise EnvironmentProviderError(
                "Docker create failed: %s" % _decode(create.stderr).strip()
            )
        try:
            start = self._run((self.docker_command, "start", name), 30.0)
            if start.returncode != 0:
                raise EnvironmentProviderError(
                    "Docker start failed: %s" % _decode(start.stderr).strip()
                )
            instance = EnvironmentInstance(
                provider=self.id,
                container_name=name,
                candidate_root=candidate_root,
                image_id=report.image_id,
                fingerprint=report.fingerprint,
                blueprint=blueprint,
            )
            for command in blueprint.health_checks:
                result = self._exec(instance, command)
                if result.exit_code != 0:
                    raise EnvironmentProviderError(
                        "environment health check failed: %s" % " ".join(command.argv)
                    )
            return instance
        except Exception:
            self._remove_container(name)
            raise

    def _exec(self, instance: EnvironmentInstance, command: CommandSpec) -> CommandReceipt:
        argv = [
            self.docker_command,
            "exec",
            "--workdir",
            command.cwd,
        ]
        for name, value in sorted(command.env.items()):
            argv.extend(("--env", "%s=%s" % (name, value)))
        argv.append(instance.container_name)
        argv.extend(command.argv)
        started = time.monotonic()
        try:
            result = self._run(tuple(argv), command.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            return CommandReceipt(
                argv=command.argv,
                cwd=command.cwd,
                exit_code=None,
                duration_ms=(time.monotonic() - started) * 1000.0,
                stdout=_decode(bytes(exc.stdout or b"")),
                stderr=_decode(bytes(exc.stderr or b"")),
                timed_out=True,
            )
        return CommandReceipt(
            argv=command.argv,
            cwd=command.cwd,
            exit_code=result.returncode,
            duration_ms=result.duration_ms,
            stdout=_decode(result.stdout),
            stderr=_decode(result.stderr),
        )

    def validate(
        self, instance: EnvironmentInstance, request: ValidationRequest
    ) -> ValidationReceipt:
        if instance.cleaned:
            raise EnvironmentProviderError("environment instance has already been cleaned")
        started_at = _utc_now()
        commands = []
        errors = []
        status = ValidationStatus.SUCCEEDED
        try:
            for command in request.blueprint.validation_commands:
                receipt = self._exec(instance, command)
                commands.append(receipt)
                if receipt.timed_out or receipt.exit_code != 0:
                    status = ValidationStatus.CANDIDATE_FAILED
                    break
            artifact_hashes = {}
            for relative in request.blueprint.output_paths:
                path = _safe_output_path(instance.candidate_root, relative)
                if path.is_file() and not path.is_symlink():
                    artifact_hashes[relative] = _file_hash(path)
        except (EnvironmentProviderError, OSError) as exc:
            status = ValidationStatus.INFRASTRUCTURE_FAILED
            errors.append(str(exc))
            artifact_hashes = {}
        return ValidationReceipt(
            api_version=VALIDATION_RECEIPT_API_VERSION,
            request_hash=request.request_hash,
            provider=self.id,
            status=status,
            environment_fingerprint=instance.fingerprint,
            commands=tuple(commands),
            artifact_hashes=artifact_hashes,
            infrastructure_errors=tuple(errors),
            started_at=started_at,
            finished_at=_utc_now(),
            metadata={
                "container_name": instance.container_name,
                "image_id": instance.image_id,
                "candidate_bundle_hash": request.candidate.bundle_hash,
                "blueprint_hash": request.blueprint.blueprint_hash,
            },
        )

    def _remove_container(self, name: str) -> None:
        try:
            self._run((self.docker_command, "rm", "--force", name), 30.0)
        except EnvironmentProviderError:
            pass

    def destroy(self, instance: EnvironmentInstance) -> None:
        if instance.cleaned:
            return
        result = self._run(
            (self.docker_command, "rm", "--force", instance.container_name),
            30.0,
        )
        if result.returncode != 0:
            raise EnvironmentProviderError(
                "Docker cleanup failed: %s" % _decode(result.stderr).strip()
            )
        instance.cleaned = True

    def validate_once(self, request: ValidationRequest) -> ValidationReceipt:
        started_at = _utc_now()
        instance = None
        try:
            instance = self.prepare(request)
            return self.validate(instance, request)
        except (EnvironmentProviderError, EnvironmentContractError) as exc:
            return ValidationReceipt(
                api_version=VALIDATION_RECEIPT_API_VERSION,
                request_hash=request.request_hash,
                provider=self.id,
                status=ValidationStatus.INFRASTRUCTURE_FAILED,
                environment_fingerprint=canonical_hash(
                    {
                        "provider": self.id,
                        "blueprint_hash": request.blueprint.blueprint_hash,
                        "unavailable": True,
                    }
                ),
                infrastructure_errors=(str(exc),),
                started_at=started_at,
                finished_at=_utc_now(),
                metadata={
                    "candidate_bundle_hash": request.candidate.bundle_hash,
                    "blueprint_hash": request.blueprint.blueprint_hash,
                },
            )
        finally:
            if instance is not None:
                try:
                    self.destroy(instance)
                except EnvironmentProviderError:
                    # A cleanup error cannot rewrite an already issued candidate result.
                    # Operators can recover the exact container from receipt metadata.
                    pass


def build_repository_verify_image(
    context: Path,
    *,
    tag: str = "aceval/repository-verify:local",
    runner: Optional[CommandRunner] = None,
    docker_command: str = "docker",
) -> str:
    """Build a local validator image and return its immutable image id."""

    source = Path(context).expanduser().resolve()
    dockerfile = source / "Dockerfile"
    if source.is_symlink() or not source.is_dir() or not dockerfile.is_file():
        raise EnvironmentProviderError("repository verifier context requires a Dockerfile")
    command_runner = runner or SubprocessCommandRunner()
    build = command_runner.run(
        (docker_command, "build", "--pull=false", "--tag", tag, str(source)),
        timeout_seconds=900.0,
    )
    if build.returncode != 0:
        raise EnvironmentProviderError(
            "Docker image build failed: %s" % _decode(build.stderr).strip()
        )
    inspect = command_runner.run(
        (docker_command, "image", "inspect", tag, "--format", "{{.Id}}"),
        timeout_seconds=30.0,
    )
    if inspect.returncode != 0:
        raise EnvironmentProviderError("cannot inspect the built Docker image")
    return normalize_sha256(_decode(inspect.stdout).strip(), "built Docker image id")


__all__ = [
    "CapabilityReport",
    "CommandRunner",
    "EnvironmentInstance",
    "EnvironmentProviderError",
    "EnvironmentUnavailableError",
    "LocalDockerProvider",
    "ProcessResult",
    "SubprocessCommandRunner",
    "build_repository_verify_image",
]
