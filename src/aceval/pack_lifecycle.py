"""EvalPack calibration lifecycle and tamper-evident freeze locks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Mapping, Optional


CALIBRATION_DRAFT = "draft"
CALIBRATION_CALIBRATING = "calibrating"
CALIBRATION_FROZEN = "frozen"
CALIBRATION_LEGACY = "legacy"
CALIBRATION_STATUSES = frozenset(
    (CALIBRATION_DRAFT, CALIBRATION_CALIBRATING, CALIBRATION_FROZEN)
)

PACK_LOCK_FILE = ".aceval-pack-lock.json"
PACK_LOCK_API_VERSION = "aceval.pack-lock/v1"


class PackLifecycleError(ValueError):
    """The declared Pack lifecycle or freeze lock is invalid."""


def calibration_status_from_metadata(metadata: Any) -> str:
    """Return an explicit lifecycle state, or ``legacy`` when undeclared."""

    extra = getattr(metadata, "extra", metadata)
    if not isinstance(extra, Mapping):
        raise PackLifecycleError("Pack metadata must be an object")
    value = extra.get("calibration_status")
    if value is None:
        return CALIBRATION_LEGACY
    if not isinstance(value, str) or value not in CALIBRATION_STATUSES:
        raise PackLifecycleError(
            "metadata.calibration_status must be draft, calibrating, or frozen"
        )
    return value


def pack_calibration_status(pack: Any) -> str:
    manifest = getattr(pack, "manifest", pack)
    metadata = getattr(manifest, "metadata", None)
    return calibration_status_from_metadata(metadata)


def verify_pack_lifecycle(root: Path, metadata: Any) -> str:
    """Validate lifecycle metadata and, for frozen Packs, its content lock."""

    root = Path(root)
    status = calibration_status_from_metadata(metadata)
    lock_path = root / PACK_LOCK_FILE
    if status != CALIBRATION_FROZEN:
        if lock_path.exists() or lock_path.is_symlink():
            raise PackLifecycleError(
                "%s is only valid when metadata.calibration_status is frozen"
                % PACK_LOCK_FILE
            )
        return status

    if lock_path.is_symlink() or not lock_path.is_file():
        raise PackLifecycleError(
            "frozen EvalPack is missing its %s content lock" % PACK_LOCK_FILE
        )
    try:
        lock = json.loads(
            lock_path.read_text(encoding="utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PackLifecycleError("cannot read frozen EvalPack lock: %s" % exc) from exc
    if not isinstance(lock, Mapping):
        raise PackLifecycleError("frozen EvalPack lock must contain an object")
    allowed = {
        "api_version",
        "calibration_status",
        "definition_hash",
        "files",
        "pack_version",
    }
    unknown = set(lock).difference(allowed)
    if unknown:
        raise PackLifecycleError(
            "frozen EvalPack lock contains unsupported field(s): %s"
            % ", ".join(sorted(str(item) for item in unknown))
        )
    if lock.get("api_version") != PACK_LOCK_API_VERSION:
        raise PackLifecycleError("unsupported frozen EvalPack lock api_version")
    if lock.get("calibration_status") != CALIBRATION_FROZEN:
        raise PackLifecycleError("frozen EvalPack lock has the wrong lifecycle state")
    declared_version = getattr(metadata, "version", None)
    if declared_version is not None and lock.get("pack_version") != declared_version:
        raise PackLifecycleError("frozen EvalPack lock pack_version is invalid")
    files = lock.get("files")
    if not isinstance(files, Mapping) or any(
        not isinstance(path, str)
        or not isinstance(digest, str)
        or not _is_sha256(digest)
        for path, digest in files.items()
    ):
        raise PackLifecycleError("frozen EvalPack lock files must map paths to sha256")
    current_files = hash_pack_definition(root)
    expected_files = {str(path): str(digest) for path, digest in files.items()}
    if current_files != expected_files:
        raise PackLifecycleError(
            "frozen EvalPack contents differ from its calibration lock; create a new "
            "draft/version and rerun the Skill baseline"
        )
    expected_hash = definition_hash(current_files)
    if not _is_sha256(lock.get("definition_hash")) or lock.get(
        "definition_hash"
    ) != expected_hash:
        raise PackLifecycleError("frozen EvalPack lock definition_hash is invalid")
    return status


def build_pack_lock(root: Path, pack_version: Optional[str] = None) -> Dict[str, Any]:
    files = hash_pack_definition(Path(root))
    return {
        "api_version": PACK_LOCK_API_VERSION,
        "calibration_status": CALIBRATION_FROZEN,
        "pack_version": str(pack_version or ""),
        "definition_hash": definition_hash(files),
        "files": files,
    }


def write_pack_lock(root: Path, pack_version: Optional[str] = None) -> Path:
    path = Path(root) / PACK_LOCK_FILE
    payload = (
        json.dumps(
            build_pack_lock(Path(root), pack_version),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write(path, payload)
    return path


def hash_pack_definition(root: Path) -> Dict[str, str]:
    """Hash every Pack file except the self-referential freeze lock."""

    result = {}  # type: Dict[str, str]
    try:
        entries = sorted(Path(root).rglob("*"))
        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            if relative == PACK_LOCK_FILE:
                continue
            if entry.is_symlink():
                raise PackLifecycleError(
                    "EvalPack freeze definitions cannot contain symlinks: %s" % relative
                )
            if entry.is_file():
                result[relative] = hashlib.sha256(entry.read_bytes()).hexdigest()
    except OSError as exc:
        raise PackLifecycleError("cannot hash EvalPack definition: %s" % exc) from exc
    return result


def definition_hash(files: Mapping[str, str]) -> str:
    encoded = json.dumps(
        dict(files),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reject_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant: %s" % value)


def _unique_object(pairs: Any) -> Dict[str, Any]:
    result = {}  # type: Dict[str, Any]
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: %s" % key)
        result[key] = value
    return result


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s-" % path.name, dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "CALIBRATION_DRAFT",
    "CALIBRATION_CALIBRATING",
    "CALIBRATION_FROZEN",
    "CALIBRATION_LEGACY",
    "CALIBRATION_STATUSES",
    "PACK_LOCK_FILE",
    "PACK_LOCK_API_VERSION",
    "PackLifecycleError",
    "calibration_status_from_metadata",
    "pack_calibration_status",
    "verify_pack_lifecycle",
    "build_pack_lock",
    "write_pack_lock",
    "hash_pack_definition",
    "definition_hash",
]
