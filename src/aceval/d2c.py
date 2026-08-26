"""Deterministic local-browser validation contracts for D2C Skills.

The evaluator talks to a browser driver through a small JSON stdin/stdout
protocol.  This keeps browser execution replaceable (local Chrome today,
remote validation worker later) and keeps browser failures distinct from
candidate failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from .environment_contracts import ValidationStatus, canonical_hash


D2C_PROFILE_API_VERSION = "aceval.d2c-browser-profile/v1"
D2C_REQUEST_API_VERSION = "aceval.d2c-validation-request/v1"
D2C_RECEIPT_API_VERSION = "aceval.d2c-validation-receipt/v1"
D2C_DRIVER_API_VERSION = "aceval.d2c-browser-driver/v1"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_NAMES = (
    "screenshot.png",
    "visual-diff.png",
    "dom.html",
    "console.json",
    "network.json",
    "driver-result.json",
)


class D2CError(ValueError):
    """A D2C configuration or request is unsafe or malformed."""


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise D2CError("%s must be a JSON object" % label)
    return value


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise D2CError("%s must be a trimmed non-empty string" % label)
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise D2CError("%s must be between %d and %d" % (label, minimum, maximum))
    return value


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise D2CError("%s must be a number" % label)
    result = float(value)
    if not minimum <= result <= maximum:
        raise D2CError("%s must be between %s and %s" % (label, minimum, maximum))
    return result


def _load(path: Union[str, Path], label: str) -> Mapping[str, Any]:
    source = Path(path).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise D2CError("%s must be a regular JSON file" % label)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise D2CError("cannot read %s" % label) from exc
    return _object(value, label)


def _builtin_driver() -> Path:
    return Path(__file__).resolve().parent / "d2c_driver" / "driver.mjs"


@dataclass(frozen=True)
class D2CBrowserProfile:
    api_version: str
    name: str
    node_executable: str
    chrome_executable: str
    driver_path: Optional[str] = None
    viewport_width: int = 1440
    viewport_height: int = 900
    device_scale_factor: float = 1.0
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    color_scheme: str = "light"
    stability_wait_ms: int = 800
    timeout_seconds: int = 60

    def __post_init__(self) -> None:
        if self.api_version != D2C_PROFILE_API_VERSION:
            raise D2CError("unsupported D2C profile api_version")
        _text(self.name, "profile.name", 128)
        _text(self.node_executable, "profile.node_executable")
        _text(self.chrome_executable, "profile.chrome_executable")
        if self.driver_path is not None:
            _text(self.driver_path, "profile.driver_path")
        _integer(self.viewport_width, "profile.viewport_width", 320, 7680)
        _integer(self.viewport_height, "profile.viewport_height", 320, 4320)
        _number(self.device_scale_factor, "profile.device_scale_factor", 0.5, 4.0)
        _text(self.locale, "profile.locale", 64)
        _text(self.timezone, "profile.timezone", 128)
        if self.color_scheme not in ("light", "dark"):
            raise D2CError("profile.color_scheme must be light or dark")
        _integer(self.stability_wait_ms, "profile.stability_wait_ms", 0, 30_000)
        _integer(self.timeout_seconds, "profile.timeout_seconds", 1, 600)

    @property
    def resolved_driver_path(self) -> Path:
        return Path(self.driver_path).expanduser().resolve() if self.driver_path else _builtin_driver()

    @classmethod
    def from_mapping(cls, value: Any) -> "D2CBrowserProfile":
        item = _object(value, "D2C profile")
        allowed = {
            "api_version", "name", "node_executable", "chrome_executable", "driver_path",
            "viewport_width", "viewport_height", "device_scale_factor", "locale", "timezone",
            "color_scheme", "stability_wait_ms", "timeout_seconds",
        }
        unknown = sorted(set(item).difference(allowed))
        if unknown:
            raise D2CError("D2C profile contains unsupported fields: %s" % ", ".join(unknown))
        return cls(
            api_version=item.get("api_version", ""),
            name=item.get("name", ""),
            node_executable=item.get("node_executable", ""),
            chrome_executable=item.get("chrome_executable", ""),
            driver_path=item.get("driver_path"),
            viewport_width=item.get("viewport_width", 1440),
            viewport_height=item.get("viewport_height", 900),
            device_scale_factor=item.get("device_scale_factor", 1.0),
            locale=item.get("locale", "zh-CN"),
            timezone=item.get("timezone", "Asia/Shanghai"),
            color_scheme=item.get("color_scheme", "light"),
            stability_wait_ms=item.get("stability_wait_ms", 800),
            timeout_seconds=item.get("timeout_seconds", 60),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "D2CBrowserProfile":
        return cls.from_mapping(_load(path, "D2C profile"))

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "name": self.name,
            "node_executable": self.node_executable,
            "chrome_executable": self.chrome_executable,
            "driver_path": self.driver_path,
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "device_scale_factor": self.device_scale_factor,
            "locale": self.locale,
            "timezone": self.timezone,
            "color_scheme": self.color_scheme,
            "stability_wait_ms": self.stability_wait_ms,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class D2CValidationRequest:
    api_version: str
    case_id: str
    url: str
    candidate_commit: str
    actions: Tuple[Mapping[str, Any], ...] = ()
    expected_title: Optional[str] = None
    visual_oracle: Optional[Mapping[str, Any]] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.api_version != D2C_REQUEST_API_VERSION:
            raise D2CError("unsupported D2C request api_version")
        _text(self.case_id, "request.case_id", 256)
        parsed = urlsplit(_text(self.url, "request.url"))
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise D2CError("request.url must be an http loopback URL")
        if not _COMMIT.fullmatch(self.candidate_commit):
            raise D2CError("request.candidate_commit must be a lowercase 40-character commit")
        normalized = []
        for index, raw in enumerate(self.actions):
            action = _object(raw, "request.actions[%d]" % index)
            action_type = action.get("type")
            if action_type not in ("click", "fill", "wait_for"):
                raise D2CError("unsupported D2C action type: %s" % action_type)
            allowed = {"type", "selector", "value", "timeout_ms"}
            unknown = sorted(set(action).difference(allowed))
            if unknown:
                raise D2CError("D2C action contains unsupported fields: %s" % ", ".join(unknown))
            selector = _text(action.get("selector"), "request.actions[].selector", 1024)
            value = action.get("value")
            if action_type == "fill" and not isinstance(value, str):
                raise D2CError("fill action requires a string value")
            timeout_ms = _integer(action.get("timeout_ms", 5000), "action.timeout_ms", 1, 60_000)
            normalized.append({"type": action_type, "selector": selector, "value": value, "timeout_ms": timeout_ms})
        object.__setattr__(self, "actions", tuple(normalized))
        if self.expected_title is not None:
            _text(self.expected_title, "request.expected_title", 512)
        if self.visual_oracle is not None:
            visual = _object(self.visual_oracle, "request.visual_oracle")
            allowed_visual = {"path", "sha256", "max_diff_ratio", "pixel_threshold"}
            unknown_visual = sorted(set(visual).difference(allowed_visual))
            if unknown_visual:
                raise D2CError("visual_oracle contains unsupported fields: %s" % ", ".join(unknown_visual))
            reference_path = Path(_text(visual.get("path"), "visual_oracle.path")).expanduser().resolve()
            digest = _text(visual.get("sha256"), "visual_oracle.sha256", 71)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise D2CError("visual_oracle.sha256 must be a lowercase SHA-256 digest")
            ratio = _number(visual.get("max_diff_ratio", 0.01), "visual_oracle.max_diff_ratio", 0.0, 1.0)
            threshold = _integer(visual.get("pixel_threshold", 16), "visual_oracle.pixel_threshold", 0, 255)
            object.__setattr__(self, "visual_oracle", {"path": str(reference_path), "sha256": digest, "max_diff_ratio": ratio, "pixel_threshold": threshold})
        _object(self.metadata, "request.metadata")
        # Verify that metadata is strict JSON before handing it to another process.
        try:
            json.dumps(self.metadata, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise D2CError("request.metadata must be strict JSON") from exc

    @property
    def request_hash(self) -> str:
        return canonical_hash(self.as_dict())

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "case_id": self.case_id,
            "url": self.url,
            "candidate_commit": self.candidate_commit,
            "actions": list(self.actions),
            "expected_title": self.expected_title,
            "visual_oracle": dict(self.visual_oracle) if self.visual_oracle else None,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "D2CValidationRequest":
        item = _object(value, "D2C request")
        allowed = {"api_version", "case_id", "url", "candidate_commit", "actions", "expected_title", "visual_oracle", "metadata"}
        unknown = sorted(set(item).difference(allowed))
        if unknown:
            raise D2CError("D2C request contains unsupported fields: %s" % ", ".join(unknown))
        actions = item.get("actions", [])
        if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
            raise D2CError("request.actions must be an array")
        return cls(
            api_version=item.get("api_version", ""),
            case_id=item.get("case_id", ""),
            url=item.get("url", ""),
            candidate_commit=item.get("candidate_commit", ""),
            actions=tuple(actions),
            expected_title=item.get("expected_title"),
            visual_oracle=item.get("visual_oracle"),
            metadata=item.get("metadata", {}),
        )

    @classmethod
    def load(cls, path: Union[str, Path]) -> "D2CValidationRequest":
        return cls.from_mapping(_load(path, "D2C request"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _executable(path_value: str, label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file() or not os.access(str(path), os.X_OK):
        raise D2CError("%s is not an executable file: %s" % (label, path))
    return path


def _node_environment(profile: D2CBrowserProfile) -> Mapping[str, str]:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    configured = os.environ.get("ACEVAL_D2C_NODE_EXECUTABLE")
    if os.environ.get("ACEVAL_D2C_USE_ELECTRON_NODE") == "1" and configured:
        try:
            if Path(configured).expanduser().resolve() == Path(profile.node_executable).expanduser().resolve():
                environment["ELECTRON_RUN_AS_NODE"] = "1"
        except OSError:
            pass
    return environment


def check_d2c_profile(profile: D2CBrowserProfile) -> Mapping[str, Any]:
    missing = []
    versions = {}
    driver = profile.resolved_driver_path
    for label, raw in (("node", profile.node_executable), ("chrome", profile.chrome_executable)):
        try:
            executable = _executable(raw, label)
            flag = "--version"
            result = subprocess.run(
                (str(executable), flag),
                capture_output=True,
                timeout=10,
                check=False,
                env=_node_environment(profile) if label == "node" else None,
            )
            versions[label] = (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
            if result.returncode != 0:
                missing.append(label)
        except (D2CError, OSError, subprocess.SubprocessError) as exc:
            missing.append("%s:%s" % (label, exc))
    if not driver.is_file():
        missing.append("driver:%s" % driver)
    fingerprint = None
    if not missing:
        fingerprint = canonical_hash({"profile": profile.as_dict(), "versions": versions, "driver_sha256": _sha256(driver)})
    return {
        "api_version": D2C_PROFILE_API_VERSION,
        "ready": not missing,
        "profile": profile.name,
        "versions": versions,
        "driver_path": str(driver),
        "missing": missing,
        "environment_fingerprint": fingerprint,
    }


def validate_d2c(
    profile: D2CBrowserProfile,
    request: D2CValidationRequest,
    output_dir: Union[str, Path],
) -> Mapping[str, Any]:
    """Run one browser validation and persist immutable evidence + receipt."""

    report = check_d2c_profile(profile)
    if not report["ready"]:
        raise D2CError("D2C browser profile is not ready: %s" % "; ".join(report["missing"]))
    output = Path(output_dir).expanduser().resolve()
    if output.is_symlink():
        raise D2CError("D2C output cannot be a symlink")
    if output.exists() and any(output.iterdir()):
        raise D2CError("D2C output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    driver_request = {
        "api_version": D2C_DRIVER_API_VERSION,
        "chrome_executable": str(Path(profile.chrome_executable).expanduser().resolve()),
        "output_dir": str(output),
        "url": request.url,
        "actions": list(request.actions),
        "expected_title": request.expected_title,
        "viewport": {"width": profile.viewport_width, "height": profile.viewport_height},
        "device_scale_factor": profile.device_scale_factor,
        "locale": profile.locale,
        "timezone": profile.timezone,
        "color_scheme": profile.color_scheme,
        "stability_wait_ms": profile.stability_wait_ms,
        "timeout_ms": max(1000, profile.timeout_seconds * 1000 - 2000),
    }
    if request.visual_oracle is not None:
        reference = Path(str(request.visual_oracle["path"]))
        if reference.is_symlink() or not reference.is_file():
            raise D2CError("visual oracle path must be a regular file")
        actual_digest = _sha256(reference)
        if actual_digest != request.visual_oracle["sha256"]:
            raise D2CError("visual oracle content does not match its frozen sha256")
        driver_request["visual_oracle"] = {
            "png_base64": base64.b64encode(reference.read_bytes()).decode("ascii"),
            "sha256": actual_digest,
            "max_diff_ratio": request.visual_oracle["max_diff_ratio"],
            "pixel_threshold": request.visual_oracle["pixel_threshold"],
        }
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    status = ValidationStatus.INFRASTRUCTURE_FAILED
    infrastructure_errors = []
    driver_result: Mapping[str, Any] = {}
    try:
        node_argv = [str(Path(profile.node_executable).expanduser().resolve())]
        if profile.resolved_driver_path.suffix == ".mjs":
            # Node 20 ships the standards-based WebSocket implementation
            # behind this flag; Node 24 accepts it as well.
            node_argv.append("--experimental-websocket")
        node_argv.append(str(profile.resolved_driver_path))
        completed = subprocess.run(
            tuple(node_argv),
            input=json.dumps(driver_request, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=profile.timeout_seconds,
            check=False,
            shell=False,
            env=_node_environment(profile),
        )
        try:
            parsed = json.loads(completed.stdout.decode("utf-8"))
            driver_result = _object(parsed, "D2C driver result")
        except (UnicodeError, json.JSONDecodeError, D2CError) as exc:
            infrastructure_errors.append("driver returned invalid JSON: %s" % exc)
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[-2000:].strip()
            infrastructure_errors.append("driver failed%s" % (": " + detail if detail else ""))
        elif driver_result.get("api_version") != D2C_DRIVER_API_VERSION:
            infrastructure_errors.append("driver api_version mismatch")
        elif driver_result.get("infrastructure_error"):
            infrastructure_errors.append(str(driver_result.get("infrastructure_error")))
        elif driver_result.get("success"):
            status = ValidationStatus.SUCCEEDED
        else:
            status = ValidationStatus.CANDIDATE_FAILED
    except subprocess.TimeoutExpired:
        infrastructure_errors.append("browser validation timed out")
    except OSError as exc:
        infrastructure_errors.append("cannot start browser driver: %s" % exc)

    artifacts = {}
    for name in _ARTIFACT_NAMES:
        path = output / name
        if path.is_file() and not path.is_symlink():
            artifacts[name] = {"path": name, "sha256": _sha256(path), "bytes": path.stat().st_size}
    receipt = {
        "api_version": D2C_RECEIPT_API_VERSION,
        "case_id": request.case_id,
        "request_hash": request.request_hash,
        "candidate_commit": request.candidate_commit,
        "status": status.value,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        "environment_fingerprint": report["environment_fingerprint"],
        "profile": profile.name,
        "artifacts": artifacts,
        "driver_summary": dict(driver_result),
        "infrastructure_errors": infrastructure_errors,
    }
    receipt["receipt_hash"] = canonical_hash(receipt)
    (output / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return receipt


__all__ = [
    "D2C_DRIVER_API_VERSION", "D2C_PROFILE_API_VERSION", "D2C_RECEIPT_API_VERSION",
    "D2C_REQUEST_API_VERSION", "D2CBrowserProfile", "D2CError", "D2CValidationRequest",
    "check_d2c_profile", "validate_d2c",
]
