"""Frozen per-iteration trial-environment contracts.

The contract deliberately describes the bytes/revisions which affect a trial,
not merely the paths from which they were loaded.  A contract is created once
before the first batch in an iteration and every later batch must match it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Optional, Sequence

from .kernel_contracts import contract_hash


TRIAL_ENVIRONMENT_CONTRACT_API_VERSION = "aceval.trial-environment-contract/v1"


class TrialEnvironmentContractError(RuntimeError):
    pass


class TrialEnvironmentContract(dict):
    """Small immutable-by-convention value wrapper for persisted contracts."""

    def __init__(self, value: Mapping[str, Any]):
        payload = dict(value)
        verify_contract_hash(payload)
        super().__init__(payload)

    @property
    def contract_hash(self) -> str:
        return str(self["contract_hash"])

    def to_dict(self) -> Mapping[str, Any]:
        return dict(self)


def _sha256_file(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
    return "sha256:" + digest


def _tree_hash(path_value: Optional[str]) -> Optional[str]:
    if not path_value:
        return None
    root = Path(path_value).expanduser().resolve()
    if root.is_file():
        root = root.parent
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    try:
        for path in sorted(root.rglob("*")):
            if ".git" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                digest.update((relative + "\0symlink\0").encode("utf-8"))
                continue
            if not path.is_file():
                continue
            digest.update(relative.encode("utf-8")); digest.update(b"\0")
            digest.update(path.read_bytes()); digest.update(b"\0")
    except OSError:
        return None
    return "sha256:" + digest.hexdigest()


def git_revision(path_value: Optional[str]) -> Optional[str]:
    if not path_value:
        return None
    root = Path(path_value).expanduser().resolve()
    if root.is_file():
        root = root.parent
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.decode("ascii", errors="ignore").strip().lower()
    return value if result.returncode == 0 and len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None


def _safe_profile_identity(path: Optional[str]) -> Mapping[str, Any]:
    """Return non-secret profile identity useful in a read model."""
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    blocked = ("token", "secret", "password", "credential", "private", "key")
    result = {}
    for key in ("api_version", "name", "id", "provider", "region", "endpoint", "profile"):
        if key not in value:
            continue
        if any(term in key.lower() for term in blocked):
            continue
        item = value[key]
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
    return result


def build_trial_environment_contract(
    config: Any,
    design: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    iteration: int,
    runtime_parameters: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    trial = getattr(config, "trial_executor", None)
    profile_path = getattr(trial, "profile_path", None) or getattr(getattr(config, "remote_agent", None), "profile_path", None)
    provider = getattr(trial, "provider", None) or "catx"
    skill = getattr(config, "skill_repository", None)
    code = getattr(config, "code_repository", None)
    cases = [x for x in design.get("cases", ()) if isinstance(x, Mapping)]

    def repository(value: Any, role: str) -> Mapping[str, Any]:
        if value is None:
            return {"role": role, "present": False}
        local = getattr(value, "local_path", None)
        return {
            "role": role,
            "ssh_url": getattr(value, "ssh_url", None),
            "branch": getattr(value, "branch", None),
            "mount_path": getattr(value, "mount_path", None),
            "revision": git_revision(local) or state.get("challenger_commit") if role == "skill" else git_revision(local),
            "working_tree_hash": _tree_hash(local),
        }

    payload = {
        "api_version": TRIAL_ENVIRONMENT_CONTRACT_API_VERSION,
        "iteration": int(iteration),
        "provider": str(provider),
        "profile": {"path": profile_path, "content_sha256": _sha256_file(profile_path), "identity": _safe_profile_identity(profile_path)},
        "skill": repository(skill, "skill"),
        "code": repository(code, "code"),
        "fixture_revisions": sorted({str((c.get("metadata") or {}).get("fixture_revision")) for c in cases if isinstance(c.get("metadata"), Mapping) and (c.get("metadata") or {}).get("fixture_revision")}),
        "repository_bindings": [
            {"role": "skill", "mount_path": getattr(skill, "mount_path", None) or "/workspace/skill"},
            *([{"role": "code", "mount_path": getattr(code, "mount_path", None) or "/workspace/repo"}] if code is not None else []),
        ],
        "design_hash": contract_hash(design),
        "case_revision_set": sorted(str(c.get("case_revision") or (c.get("metadata") or {}).get("case_revision")) for c in cases),
        "runtime_parameters": dict(runtime_parameters or {}),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # created_at is evidence metadata, not an identity input.
    identity = dict(payload); identity.pop("created_at", None)
    payload["contract_hash"] = contract_hash(identity)
    return payload


def verify_contract_hash(contract: Mapping[str, Any]) -> str:
    expected = contract_hash({key: value for key, value in contract.items() if key != "contract_hash" and key != "created_at"})
    actual = str(contract.get("contract_hash") or "")
    if actual != expected:
        raise TrialEnvironmentContractError("trial environment contract hash does not match contents")
    return actual


__all__ = ["TRIAL_ENVIRONMENT_CONTRACT_API_VERSION", "TrialEnvironmentContract", "TrialEnvironmentContractError", "build_trial_environment_contract", "verify_contract_hash", "git_revision"]
