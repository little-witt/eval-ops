"""Recoverable multi-session dispatch and complete-log collection."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time
from typing import Any, Mapping, Optional, Protocol, Sequence
import uuid

from .contracts import as_primitive
from .kernel_contracts import REMOTE_BATCH_API_VERSION
from .task_center import TaskStore
from .catx_bindings import CatxExecutionBinding


class RemoteBatchError(RuntimeError):
    pass


_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
_SAFE_REVISION = re.compile(r"^[0-9a-f]{40}$")


class SessionGateway(Protocol):
    def start_session(self, request: Mapping[str, Any], *, binding: Optional[CatxExecutionBinding] = None) -> str:
        ...

    def poll_session(self, session_id: str) -> Mapping[str, Any]:
        ...

    def fetch_session(self, session_id: str) -> Any:
        ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_ms(started_at: Any, completed_at: Any) -> Optional[int]:
    """Compute a best-effort wall duration without weakening evidence."""

    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        value = int((end - start).total_seconds() * 1000)
        return max(0, value)
    except (TypeError, ValueError, OverflowError):
        return None


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))
    path.chmod(0o600)


def _load(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteBatchError("cannot read remote batch state") from exc
    if not isinstance(value, Mapping) or value.get("api_version") != REMOTE_BATCH_API_VERSION:
        raise RemoteBatchError("invalid remote batch state")
    return value


def _safe_case_file(case_id: str) -> str:
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:10]
    slug = "".join(character if character.isalnum() or character in "-_" else "-" for character in case_id).strip("-")[:64] or "case"
    return "%s-%s.json" % (slug, digest)


def _event_log_sha256(events: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(events),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _request_fingerprint(
    *,
    purpose: str,
    prepared_cases: Sequence[tuple[str, str, str]],
    goal: str,
    standards: Sequence[str],
    repository_bindings: Sequence[Mapping[str, Any]],
    environment_contract_hash: Optional[str],
    execution_bindings: Optional[Mapping[str, CatxExecutionBinding]] = None,
) -> str:
    """Identify the exact immutable inputs of a persisted remote batch."""

    payload = {
        "purpose": str(purpose),
        "cases": [
            {"case_id": case_id, "prompt": prompt, "title": title}
            for case_id, prompt, title in prepared_cases
        ],
        "goal": str(goal),
        "standards": [str(item) for item in standards],
        "repository_bindings": [dict(item) for item in repository_bindings],
        "environment_contract_hash": environment_contract_hash,
        "execution_bindings": {
            str(case_id): _binding_evidence_payload(binding)
            for case_id, binding in sorted((execution_bindings or {}).items())
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _attempt_number(row: Mapping[str, Any]) -> int:
    value = row.get("attempt_number")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    attempts = row.get("attempts", ())
    return len(attempts) + 1 if isinstance(attempts, list) else 1


def _archive_attempt(row: dict[str, Any], *, retry_reason: Optional[str] = None) -> None:
    """Preserve a terminal remote attempt before retrying or returning it."""

    number = _attempt_number(row)
    attempts = [dict(item) for item in row.get("attempts", ()) if isinstance(item, Mapping)]
    existing = next(
        (item for item in attempts if item.get("attempt_number") == number),
        None,
    )
    if existing is not None:
        # A terminal attempt is normally archived during collection, before a
        # person asks to retry it.  Preserve that immutable attempt while
        # attaching the later retry decision to the same history row.
        if retry_reason and not existing.get("retry_reason"):
            existing["retry_reason"] = retry_reason
        row["attempts"] = attempts
        return
    attempts.append(
        {
            "attempt_number": number,
            "session_id": row.get("session_id"),
            "status": row.get("status"),
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"),
            "duration_ms": row.get("duration_ms"),
            "terminal": row.get("terminal"),
            "artifact": row.get("artifact"),
            "error": row.get("error"),
            "fetch_error": row.get("fetch_error"),
            "binding_status": row.get("binding_status"),
            "binding_evidence": row.get("binding_evidence"),
            "message_status": row.get("message_status"),
            "log_completeness": row.get("log_completeness"),
            "retry_reason": retry_reason,
        }
    )
    row["attempts"] = attempts


def _session_payload(bundle: Any) -> Mapping[str, Any]:
    if hasattr(bundle, "observation") and hasattr(bundle, "completeness"):
        return {
            "schema_version": "aceval.imported-session/v1",
            "session_id": str(bundle.session_id),
            "profile_name": str(bundle.profile_name),
            "source": str(bundle.source),
            "completeness": dict(bundle.completeness.as_dict()),
            "observation": as_primitive(bundle.observation),
        }
    if isinstance(bundle, Mapping):
        return json.loads(json.dumps(bundle, ensure_ascii=False, allow_nan=False))
    raise RemoteBatchError("session gateway returned an unsupported bundle")


def _binding_evidence_payload(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(k): _binding_evidence_payload(v) for k, v in value.items()}
    fields = ("verified", "requested_binding_hash", "actual_subject_hash", "actual_repository_hash", "actual_base_commit", "actual_head_commit", "details")
    if all(hasattr(value, field) for field in fields):
        return {field: _binding_evidence_payload(getattr(value, field)) for field in fields}
    binding_fields = ("api_version", "subject_hash", "skill_ref", "repository_ref", "repository_hash", "base_commit", "head_commit", "binding_hash", "metadata")
    if all(hasattr(value, field) for field in binding_fields):
        return {field: _binding_evidence_payload(getattr(value, field)) for field in binding_fields}
    if isinstance(value, (list, tuple)):
        return [_binding_evidence_payload(item) for item in value]
    return value


def compact_remote_prompt(
    case: Mapping[str, Any],
    *,
    goal: str,
    standards: Sequence[str],
    repository_bindings: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Build an evaluation-blind online prompt.

    The remote worker receives the concrete Case and immutable repository
    bindings only.  The optimization goal and acceptance standards belong to
    the local control plane and must never bias the worker's answer.
    """

    del goal, standards

    prompt = str(case.get("prompt") or "").strip()
    if not prompt:
        raise RemoteBatchError("remote case prompt must not be empty")
    metadata = case.get("metadata", {}) if isinstance(case.get("metadata"), Mapping) else {}
    repository_lines = []
    repository_checks = []
    for binding in repository_bindings:
        role = str(binding.get("role") or "repository")
        if role not in ("skill", "code"):
            raise RemoteBatchError("repository binding role is invalid")
        branch = str(binding.get("branch") or "").strip()
        if role == "code" and (metadata.get("head_ref") or metadata.get("fixture_branch")):
            branch = str(metadata.get("head_ref") or metadata.get("fixture_branch")).strip()
        mount_path = str(binding.get("mount_path") or "").strip()
        revision = str(binding.get("revision") or "").strip().lower()
        if (
            branch
            and (
                not _SAFE_BRANCH.fullmatch(branch)
                or ".." in branch
                or "//" in branch
                or "@{" in branch
                or branch.endswith((".", "/", ".lock"))
            )
        ):
            raise RemoteBatchError("repository binding branch is unsafe")
        if mount_path and (not mount_path.startswith("/") or "\n" in mount_path or "\r" in mount_path):
            raise RemoteBatchError("repository binding mount_path is unsafe")
        if revision and not _SAFE_REVISION.fullmatch(revision):
            raise RemoteBatchError("repository binding revision must be a full Git commit SHA")
        if branch and mount_path:
            quoted_mount = shlex.quote(mount_path)
            quoted_branch = shlex.quote(branch)
            base_commit = str(metadata.get("base_commit") or "").strip().lower() if role == "code" else ""
            head_commit = str(metadata.get("head_commit") or "").strip().lower() if role == "code" else ""
            if bool(base_commit) != bool(head_commit):
                raise RemoteBatchError("code-review Case must provide both base_commit and head_commit")
            if base_commit and (not _SAFE_REVISION.fullmatch(base_commit) or not _SAFE_REVISION.fullmatch(head_commit)):
                raise RemoteBatchError("code-review Case commits must be full Git commit SHAs")
            if base_commit == head_commit and base_commit:
                raise RemoteBatchError("code-review Case base_commit and head_commit must differ")
            if role == "code" and base_commit and head_commit:
                repository_lines.append(
                    "- code: 在 %s 评审 PR %s..%s（远端分支 %s）"
                    % (mount_path, base_commit, head_commit, branch)
                )
                repository_checks.append(
                    "- 必须依次执行 `git -C %s fetch --no-tags origin %s` 和 `git -C %s rev-parse FETCH_HEAD`，确认结果等于 `%s`；"
                    "再执行 `git -C %s checkout --detach %s` 和 `git -C %s rev-parse HEAD`，确认结果等于 `%s`；"
                    "最后执行 `git -C %s diff --binary %s %s | git -C %s apply`，仅评审工作区中的 PR diff，不得修改源码。"
                    % (
                        quoted_mount, quoted_branch, quoted_mount, head_commit,
                        quoted_mount, base_commit, quoted_mount, base_commit,
                        quoted_mount, base_commit, head_commit, quoted_mount,
                    )
                )
                continue
            if revision:
                repository_lines.append("- %s: 在 %s 检出提交 %s（分支 %s 仅用于定位仓库）" % (role, mount_path, revision, branch))
                repository_checks.append(
                    "- 必须依次执行 `git -C %s fetch --no-tags origin %s`、`git -C %s checkout --detach %s`、"
                    "`git -C %s rev-parse HEAD`；最后结果必须等于 `%s`，否则立即停止并报告绑定失败。"
                    % (quoted_mount, quoted_branch, quoted_mount, revision, quoted_mount, revision)
                )
            else:
                repository_lines.append("- %s: 在 %s 使用分支 %s" % (role, mount_path, branch))
                repository_checks.append(
                    "- 必须依次执行 `git -C %s fetch --no-tags origin %s`、`git -C %s checkout --detach FETCH_HEAD`、"
                    "`git -C %s rev-parse HEAD`，并把实际 commit 写入日志。"
                    % (quoted_mount, quoted_branch, quoted_mount, quoted_mount)
                )
    repository_text = (
        "\n仓库绑定：\n" + "\n".join(repository_lines)
        + "\n开始任务前的强制准备与核验（这些命令及结果必须进入完整工具日志）：\n"
        + "\n".join(repository_checks)
    ) if repository_lines else ""
    if metadata.get("harness_baseline") == "without_skill":
        return (
            "请自然完成下面任务，但不得读取或使用挂载仓库中的 SKILL.md、references 或 scripts。"
            "完整保留工具调用与结果。\n\n任务：\n%s%s" % (prompt, repository_text)
        )
    if metadata.get("harness_case_kind") == "negative_trigger":
        return (
            "请自然完成下面任务；只有当挂载 Skill 的描述确实适用于任务时才读取并使用它，"
            "不适用时不得读取 SKILL.md，也不得强行套用其流程。完整保留工具调用与结果。\n\n"
            "任务：\n%s%s" % (prompt, repository_text)
        )
    task_kind = "使用目标 Skill 评审下面 PR（测试仓库的独立 Case 分支）" if metadata.get("base_commit") and metadata.get("head_commit") else "使用目标 Skill 完成下面 Case"
    return (
        "%s。先准备下列仓库并从 skill 挂载点读取 SKILL.md（若入口位于 src/SKILL.md 则读取该文件），"
        "按其关键步骤执行；不要修改或发布 Skill，不要修改被评审源码。完整保留工具调用与结果。\n\n"
        "Case：\n%s%s" % (task_kind, prompt, repository_text)
    )


class RemoteBatchCoordinator:
    """Starts all sessions first, then polls/fetches them concurrently.

    Batch state is persisted after every transition, so a desktop process can
    restart and continue collection without creating duplicate sessions.
    """

    def __init__(
        self,
        gateway: SessionGateway,
        store: TaskStore,
        *,
        max_parallel: int = 8,
        poll_interval_seconds: int = 5,
        max_wait_seconds: int = 1200,
        max_prompt_chars: int = 12_000,
    ) -> None:
        if max_parallel <= 0 or poll_interval_seconds <= 0 or max_wait_seconds <= 0 or max_prompt_chars <= 0:
            raise ValueError("remote batch limits must be positive")
        self.gateway = gateway
        self.store = store
        self.max_parallel = max_parallel
        self.poll_interval_seconds = poll_interval_seconds
        self.max_wait_seconds = max_wait_seconds
        self.max_prompt_chars = max_prompt_chars

    def batch_path(self, task_id: str, iteration: int, purpose: str) -> Path:
        safe_purpose = "".join(item if item.isalnum() or item in "-_" else "-" for item in purpose).strip("-")
        if not safe_purpose:
            raise RemoteBatchError("batch purpose is invalid")
        return self.store.task_dir(task_id) / "iterations" / ("iteration-%03d" % iteration) / "batches" / (safe_purpose + ".json")

    def dispatch(
        self,
        task_id: str,
        iteration: int,
        purpose: str,
        cases: Sequence[Mapping[str, Any]],
        *,
        goal: str,
        standards: Sequence[str],
        repository_bindings: Sequence[Mapping[str, Any]] = (),
        environment_contract_hash: Optional[str] = None,
        execution_binding: Optional[CatxExecutionBinding] = None,
        execution_bindings: Optional[Mapping[str, CatxExecutionBinding]] = None,
    ) -> Mapping[str, Any]:
        if not cases:
            raise RemoteBatchError("remote batch requires at least one case")
        prepared_cases = []
        for case in cases:
            case_id = str(case.get("id") or "").strip()
            if not case_id:
                raise RemoteBatchError("remote case id must not be empty")
            prompt = compact_remote_prompt(case, goal=goal, standards=standards, repository_bindings=repository_bindings)
            if len(prompt) > self.max_prompt_chars:
                raise RemoteBatchError("remote prompt exceeds the configured Token budget")
            prepared_cases.append((case_id, prompt, str(case.get("title") or ((case.get("metadata") or {}).get("aceval_test", {}) or {}).get("title") or case_id)))
        requested_ids = [item[0] for item in prepared_cases]
        if len(requested_ids) != len(set(requested_ids)):
            raise RemoteBatchError("remote batch case ids must be unique")
        case_bindings = dict(execution_bindings or {})
        if execution_binding is not None:
            for case_id in requested_ids:
                case_bindings.setdefault(case_id, execution_binding)
        unknown_binding_ids = sorted(set(case_bindings).difference(requested_ids))
        if unknown_binding_ids:
            raise RemoteBatchError("execution bindings reference unknown cases: %s" % ", ".join(unknown_binding_ids))
        request_fingerprint = _request_fingerprint(
            purpose=purpose,
            prepared_cases=prepared_cases,
            goal=goal,
            standards=standards,
            repository_bindings=repository_bindings,
            environment_contract_hash=environment_contract_hash,
            execution_bindings=case_bindings,
        )
        path = self.batch_path(task_id, iteration, purpose)
        if path.is_file():
            loaded = dict(_load(path))
            if loaded.get("environment_contract_hash") != environment_contract_hash:
                raise RemoteBatchError("persisted batch environment contract does not match requested contract")
            persisted_ids = [str(row.get("case_id") or "") for row in loaded.get("cases", ()) if isinstance(row, Mapping)]
            if any(case_id not in requested_ids for case_id in persisted_ids):
                raise RemoteBatchError("persisted batch does not match requested cases")
            if loaded.get("status") != "dispatching" and set(persisted_ids) != set(requested_ids):
                raise RemoteBatchError("completed persisted batch does not contain exactly the requested cases")
            persisted_rows = [row for row in loaded.get("cases", ()) if isinstance(row, Mapping)]
            # A previous process can have persisted a terminal batch whose
            # every Case failed before a session id was created (for example,
            # an old run that did not yet have per-Case PR bindings).  Once
            # the missing fixture/binding material is available, that batch
            # must be recoverable instead of being treated as an idempotent
            # success.  Successful or still-running rows remain immutable and
            # continue to reject input drift.
            fingerprint_changed = bool(
                loaded.get("request_fingerprint")
                and loaded.get("request_fingerprint") != request_fingerprint
            )
            all_terminal_failures = bool(persisted_rows) and all(
                str(row.get("status") or "") == "failed" for row in persisted_rows
            )
            if fingerprint_changed and not (
                loaded.get("status") == "completed"
                and all_terminal_failures
                and set(persisted_ids) == set(requested_ids)
            ):
                raise RemoteBatchError("persisted batch request does not match the requested cases, prompts, or repository bindings")
            if fingerprint_changed and all_terminal_failures:
                requested_by_id = {case_id: (prompt, title) for case_id, prompt, title in prepared_cases}
                for row in persisted_rows:
                    case_id = str(row.get("case_id") or "")
                    prompt, title = requested_by_id[case_id]
                    row.update({
                        "case_title": title,
                        "prompt": prompt,
                        "repository_bindings": [dict(item) for item in repository_bindings],
                        "execution_binding": _binding_evidence_payload(case_bindings.get(case_id)),
                    })
                loaded["cases"] = persisted_rows
                loaded["request_fingerprint"] = request_fingerprint
            if loaded.get("status") == "dispatching":
                persisted_prompts = {
                    str(row.get("case_id")): str(row.get("prompt"))
                    for row in loaded.get("cases", ())
                    if isinstance(row, Mapping)
                    and isinstance(row.get("prompt"), str)
                    and row.get("prompt")
                }
                requested_prompts = {case_id: prompt for case_id, prompt, _ in prepared_cases}
                if any(case_id in persisted_prompts and persisted_prompts[case_id] != prompt for case_id, prompt in requested_prompts.items()):
                    raise RemoteBatchError("interrupted persisted batch prompt does not match the requested Case")
            if loaded.get("status") != "dispatching":
                if loaded.get("status") == "completed" and any(
                    str(row.get("status") or "") == "failed" for row in persisted_rows
                ):
                    # Retry only failed rows; completed evidence is never
                    # recreated.  This is the continuation path for legacy
                    # batches that reached the remote node without creating
                    # sessions.
                    _atomic_json(path, loaded)
                    return self.retry_failed(
                        task_id,
                        iteration,
                        purpose,
                        execution_bindings=case_bindings,
                    )
                return loaded
            batch = loaded
            case_states = [dict(item) for item in loaded.get("cases", ()) if isinstance(item, Mapping)]
            # The API has no idempotency key. A crash while a session is in
            # `starting` has an unknowable outcome, so fail it closed instead
            # of risking a duplicate remote session.
            for row in case_states:
                if row.get("status") == "starting":
                    row.update({"status": "failed", "error": "dispatch was interrupted before the session id was persisted; duplicate creation was refused", "completed_at": _now()})
                    _archive_attempt(row)
                    self.store.append_event(task_id, "case_run.failed", {"purpose": purpose, "error": row["error"]}, iteration=iteration, case_id=row.get("case_id"), run_id=purpose)
            batch["cases"] = case_states
            _atomic_json(path, batch)
        else:
            case_states = []
            batch = {
                "api_version": REMOTE_BATCH_API_VERSION,
                "task_id": task_id,
                "iteration": iteration,
                "purpose": purpose,
                "environment_contract_hash": environment_contract_hash,
                "request_fingerprint": request_fingerprint,
                "status": "dispatching",
                "created_at": _now(),
                "updated_at": _now(),
                "deadline_at_epoch": time.time() + self.max_wait_seconds,
                "cases": case_states,
            }
            if execution_binding is not None and not execution_bindings:
                batch["execution_binding"] = _binding_evidence_payload(execution_binding)
            if case_bindings:
                batch["execution_bindings"] = {
                    case_id: _binding_evidence_payload(binding)
                    for case_id, binding in case_bindings.items()
                }
            _atomic_json(path, batch)
        # Starting every session before polling gives real fan-out without
        # adding thread-safety assumptions to a company API client.
        persisted_ids = {str(row.get("case_id")) for row in case_states}
        for case_id, prompt, case_title in prepared_cases:
            if case_id in persisted_ids:
                continue
            case_binding = case_bindings.get(case_id)
            row = {
                "case_id": case_id,
                "case_title": case_title,
                "status": "starting",
                "session_id": None,
                "prompt": prompt,
                "environment_contract_hash": environment_contract_hash,
                "started_at": _now(),
                "completed_at": None,
                "terminal": None,
                "artifact": None,
                "error": None,
                "repository_bindings": [dict(item) for item in repository_bindings],
                "binding_status": "pending" if case_binding is not None else "not_requested",
                "binding_evidence": None,
                "execution_binding": _binding_evidence_payload(case_binding),
                "message_status": "pending",
                "log_completeness": None,
                "attempt_number": 1,
                "attempts": [],
            }
            case_states.append(row)
            persisted_ids.add(case_id)
            _atomic_json(path, batch)
            try:
                request = {"title": "aceval %s %s" % (purpose, case_id), "prompt": prompt}
                if case_binding is not None:
                    start = getattr(self.gateway, "start_session", None)
                    if not callable(start):
                        raise RemoteBatchError("session gateway cannot create a bound session")
                    session_id = start(request, binding=case_binding)
                    evidence = getattr(self.gateway, "last_binding_evidence", None)
                    verify = getattr(self.gateway, "verify_binding", None)
                    if callable(verify):
                        evidence = verify(session_id, case_binding)
                    if evidence is None:
                        raise RemoteBatchError("session binding was not verified")
                    if hasattr(evidence, "assert_matches"):
                        evidence.assert_matches(case_binding)
                    row.update({"binding_status": "verified", "binding_evidence": _binding_evidence_payload(evidence)})
                else:
                    session_id = self.gateway.start_session(request)
                row["message_status"] = "sent"
                row.update({"session_id": session_id, "status": "running"})
                self.store.append_event(task_id, "case_run.started", {"purpose": purpose, "session_id": session_id, "binding_status": row.get("binding_status"), "message_status": row.get("message_status")}, iteration=iteration, case_id=case_id, run_id=purpose)
            except Exception as exc:
                row.update({"status": "failed", "error": str(exc), "binding_status": "failed" if case_binding is not None else row.get("binding_status"), "message_status": "failed", "completed_at": _now()})
                _archive_attempt(row)
                self.store.append_event(task_id, "case_run.failed", {"purpose": purpose, "error": str(exc)}, iteration=iteration, case_id=case_id, run_id=purpose)
            batch["updated_at"] = _now()
            _atomic_json(path, batch)
        batch["status"] = "running" if any(row["status"] == "running" for row in case_states) else "completed"
        batch["updated_at"] = _now()
        _atomic_json(path, batch)
        self.store.append_event(task_id, "remote.batch_dispatched", {"purpose": purpose, "case_count": len(case_states), "batch": str(path)}, iteration=iteration)
        return batch

    def collect_once(self, task_id: str, iteration: int, purpose: str) -> Mapping[str, Any]:
        path = self.batch_path(task_id, iteration, purpose)
        batch = dict(_load(path))
        rows = [dict(item) for item in batch.get("cases", ())]
        if batch.get("status") == "completed":
            return batch
        if time.time() > float(batch.get("deadline_at_epoch", 0)):
            for row in rows:
                if row.get("status") == "running":
                    row.update({"status": "failed", "error": "remote batch deadline exceeded", "completed_at": _now()})
                    _archive_attempt(row)
                    self.store.append_event(task_id, "case_run.failed", {"purpose": purpose, "error": row["error"], "session_id": row.get("session_id")}, iteration=iteration, case_id=row.get("case_id"), run_id=purpose)
            batch.update({"cases": rows, "status": "completed", "updated_at": _now()})
            _atomic_json(path, batch)
            return batch

        running = [row for row in rows if row.get("status") == "running"]
        statuses = {}
        with ThreadPoolExecutor(max_workers=min(self.max_parallel, max(1, len(running)))) as pool:
            futures = {pool.submit(self.gateway.poll_session, str(row["session_id"])): str(row["session_id"]) for row in running}
            for future in as_completed(futures):
                session_id = futures[future]
                try:
                    statuses[session_id] = future.result()
                except Exception as exc:
                    statuses[session_id] = {"status": "RUNNING", "poll_error": str(exc)}

        for row in rows:
            if row.get("status") != "running":
                continue
            terminal = statuses.get(str(row.get("session_id")), {"status": "RUNNING"})
            row["terminal"] = terminal
            status = terminal.get("status")
            if status not in ("COMPLETED", "FAILED"):
                continue
            case_id = str(row["case_id"])
            attempt_number = _attempt_number(row)
            artifact = (
                path.parent.parent
                / "runs"
                / purpose
                / Path(_safe_case_file(case_id)).stem
                / ("attempt-%03d.json" % attempt_number)
            )
            try:
                bundle = self.gateway.fetch_session(str(row["session_id"]))
                raw_events = None
                event_integrity = None
                last_event_log = getattr(self.gateway, "last_event_log", None)
                if callable(last_event_log):
                    cached = last_event_log(str(row["session_id"]))
                    if isinstance(cached, Mapping):
                        cached_events = cached.get("events")
                        if isinstance(cached_events, Sequence) and not isinstance(cached_events, (str, bytes)):
                            raw_events = [dict(item) for item in cached_events if isinstance(item, Mapping)]
                        if isinstance(cached.get("integrity"), Mapping):
                            event_integrity = dict(cached["integrity"])
                if raw_events is None:
                    fetch_events = getattr(self.gateway, "fetch_events", None)
                    if callable(fetch_events):
                        raw_events = [dict(item) for item in fetch_events(str(row["session_id"]))]
                payload = {
                    "api_version": "aceval.kernel-case-run/v1",
                    "task_id": task_id,
                    "iteration": iteration,
                    "attempt_number": attempt_number,
                    "purpose": purpose,
                    "environment_contract_hash": batch.get("environment_contract_hash"),
                    "case_id": case_id,
                    "request": {"prompt": row["prompt"]},
                    "repository_bindings": row.get("repository_bindings", []),
                    "binding_status": row.get("binding_status"),
                    "binding_evidence": row.get("binding_evidence"),
                    "message_status": row.get("message_status"),
                    "terminal": terminal,
                    "session": _session_payload(bundle),
                }
                session_source = str(
                    payload["session"].get("source")
                    if isinstance(payload.get("session"), Mapping)
                    else ""
                )
                catx_session = session_source in {"catx_session_api", "catx"}
                if raw_events is not None:
                    payload["events"] = raw_events
                    actual_event_hash = _event_log_sha256(raw_events)
                    if event_integrity is None:
                        event_types = sorted({str(item.get("type") or item.get("kind") or "") for item in raw_events})
                        missing = [kind for kind in ("user.message", "agent.message") if kind not in event_types]
                        legacy = not catx_session
                        event_integrity = {
                            "complete": not missing and not catx_session,
                            "reason_codes": (["required_event_type_missing"] if missing else []) + (["event_log_hash_missing"] if catx_session else []),
                            "page_count": 1,
                            "event_count": len(raw_events),
                            "event_types": event_types,
                            "required_event_types": ["user.message", "agent.message"],
                            "missing_required_event_types": missing,
                            "event_log_sha256": None if catx_session else actual_event_hash,
                            "computed_event_log_sha256": actual_event_hash,
                            "legacy_event_log": legacy,
                            "promotion_eligible": not legacy,
                        }
                    elif event_integrity.get("event_log_sha256") not in (None, actual_event_hash):
                        event_integrity = dict(event_integrity)
                        event_integrity["complete"] = False
                        event_integrity["reason_codes"] = list(dict.fromkeys(
                            list(event_integrity.get("reason_codes", ())) + ["event_log_hash_mismatch"]
                        ))
                    elif catx_session and not event_integrity.get("event_log_sha256"):
                        event_integrity = dict(event_integrity)
                        event_integrity["complete"] = False
                        event_integrity["reason_codes"] = list(dict.fromkeys(
                            list(event_integrity.get("reason_codes", ())) + ["event_log_hash_missing"]
                        ))
                    payload["log_completeness"] = {
                        "raw_events": True,
                        **event_integrity,
                        # Keep the server-provided seal distinct from a hash
                        # computed locally.  For a CATX receipt, inventing a
                        # missing server seal would turn an incomplete log
                        # into apparently complete evidence.
                        "event_log_sha256": (
                            event_integrity.get("event_log_sha256")
                            if catx_session
                            else actual_event_hash
                        ),
                        "computed_event_log_sha256": actual_event_hash,
                    }
                    # CATX does not echo repository binding metadata in the
                    # session document. Upgrade request-level evidence using
                    # the authoritative tool event log before scoring.
                    binding_payload = batch.get("execution_binding")
                    verify_events = getattr(self.gateway, "verify_binding_events", None)
                    if binding_payload and callable(verify_events):
                        try:
                            binding = CatxExecutionBinding.from_mapping(binding_payload)
                            evidence = verify_events(str(row["session_id"]), binding, raw_events)
                            row["binding_evidence"] = _binding_evidence_payload(evidence)
                            row["binding_status"] = "verified" if bool(getattr(evidence, "verified", False)) else "failed"
                        except Exception as exc:
                            row.update({
                                "binding_status": "failed",
                                "binding_evidence": None,
                                "binding_error": "CATX event-log binding verification failed: %s" % str(exc)[:500],
                            })
                else:
                    # A real CATX session must expose the immutable event
                    # snapshot.  A gateway that cannot expose events is only
                    # accepted as an explicitly marked local legacy fixture;
                    # it must never be mistaken for a complete CATX receipt.
                    legacy = not catx_session and not callable(getattr(self.gateway, "fetch_events", None)) and not callable(getattr(self.gateway, "last_event_log", None))
                    payload["log_completeness"] = {
                        "raw_events": False,
                        "complete": legacy,
                        "legacy_event_log": legacy,
                        "promotion_eligible": not legacy,
                        "reason_codes": ["legacy_event_log"] if legacy else ["event_log_missing", "event_log_hash_missing"],
                        "missing_required_event_types": [],
                        "event_count": 0,
                        "page_count": 0,
                        "sequence_status": "not_provided",
                        "event_log_sha256": None,
                    }
                payload["binding_status"] = row.get("binding_status")
                payload["binding_evidence"] = row.get("binding_evidence")
                _atomic_json(artifact, payload)
                completeness = payload.get("log_completeness") or payload["session"].get("completeness")
                final_error = payload["session"].get("observation", {}).get("error")
                if row.get("binding_status") == "failed":
                    final_error = row.get("binding_error") or "CATX event log did not prove the requested Skill/repository mounts"
                event_log_incomplete = (
                    isinstance(payload.get("log_completeness"), Mapping)
                    and payload.get("log_completeness", {}).get("complete") is False
                )
                if event_log_incomplete and not final_error:
                    codes = payload.get("log_completeness", {}).get("reason_codes", ())
                    final_error = "session event log is incomplete%s" % (
                        ": " + ", ".join(str(item) for item in codes)
                        if codes
                        else ""
                    )
                row.update({
                    "status": (
                        "completed"
                        if status == "COMPLETED"
                        and row.get("binding_status") != "failed"
                        and not event_log_incomplete
                        else "failed"
                    ),
                    "artifact": str(artifact),
                    "completed_at": _now(),
                    "error": final_error,
                    "log_completeness": completeness,
                })
                row["duration_ms"] = _duration_ms(row.get("started_at"), row.get("completed_at"))
                _archive_attempt(row)
                self.store.append_event(task_id, "trace.captured", {"purpose": purpose, "session_id": row["session_id"], "session_log": str(artifact), "completeness": payload["session"].get("completeness")}, iteration=iteration, case_id=case_id, run_id=purpose)
                self.store.append_event(task_id, "case_run.completed" if row["status"] == "completed" else "case_run.failed", {"purpose": purpose, "session_id": row["session_id"], "status": row["status"], "session_log": str(artifact), "binding_status": row.get("binding_status"), "message_status": row.get("message_status"), "log_completeness": row.get("log_completeness")}, iteration=iteration, case_id=case_id, run_id=purpose)
            except Exception as exc:
                # Fetch errors are recoverable: leave the session running so a
                # later collect call can retry complete-log retrieval.
                row["terminal"] = terminal
                row["fetch_error"] = str(exc)

        batch["cases"] = rows
        batch["updated_at"] = _now()
        if all(row.get("status") in ("completed", "failed") for row in rows):
            batch["status"] = "completed"
            self.store.append_event(task_id, "remote.batch_completed", {"purpose": purpose, "batch": str(path), "completed": sum(row.get("status") == "completed" for row in rows), "failed": sum(row.get("status") == "failed" for row in rows)}, iteration=iteration)
        else:
            batch["status"] = "running"
        _atomic_json(path, batch)
        self.store.append_event(
            task_id,
            "remote.batch_polled",
            {
                "purpose": purpose,
                "total": len(rows),
                "running": sum(row.get("status") == "running" for row in rows),
                "completed": sum(row.get("status") == "completed" for row in rows),
                "failed": sum(row.get("status") == "failed" for row in rows),
            },
            iteration=iteration,
        )
        return batch

    def retry_failed(
        self,
        task_id: str,
        iteration: int,
        purpose: str,
        *,
        execution_binding: Optional[CatxExecutionBinding] = None,
        execution_bindings: Optional[Mapping[str, CatxExecutionBinding]] = None,
        case_ids: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        """Recreate selected or transport-failed sessions and preserve history.

        ``case_ids`` is used for targeted evidence recovery.  It may include a
        completed session whose artifact was incomplete or otherwise invalid;
        the prior Attempt remains archived and only those Cases are rerun.
        """

        path = self.batch_path(task_id, iteration, purpose)
        batch = dict(_load(path))
        rows = [dict(item) for item in batch.get("cases", ()) if isinstance(item, Mapping)]
        selected = {str(item) for item in case_ids if str(item)}
        known = {str(row.get("case_id") or "") for row in rows}
        if selected.difference(known):
            raise RemoteBatchError("retry references unknown cases: %s" % ", ".join(sorted(selected.difference(known))))
        failed = [
            row for row in rows
            if str(row.get("case_id") or "") in selected
            or (not selected and row.get("status") == "failed")
        ]
        if not failed:
            raise RemoteBatchError("remote batch has no selected or failed cases to retry")
        per_case_bindings = dict(execution_bindings or {})
        for row in failed:
            case_id = str(row.get("case_id") or "")
            prompt = str(row.get("prompt") or "")
            retry_reason = str(row.get("error") or row.get("fetch_error") or ("evidence recovery requested" if selected else "failed remote attempt"))
            _archive_attempt(row, retry_reason=retry_reason)
            row_binding = per_case_bindings.get(case_id) or execution_binding
            if row_binding is None and isinstance(row.get("execution_binding"), Mapping):
                row_binding = CatxExecutionBinding.from_mapping(row["execution_binding"])
            next_attempt = _attempt_number(row) + 1
            row.update({
                "status": "starting",
                "session_id": None,
                "terminal": None,
                "artifact": None,
                "error": None,
                "fetch_error": None,
                "started_at": _now(),
                "completed_at": None,
                "attempt_number": next_attempt,
                "retry_reason": retry_reason,
                "log_completeness": None,
                "message_status": "pending",
                # A retry is a fresh transport attempt.  Do not carry a
                # previous binding failure into an intentionally unbound
                # retry (or stale evidence into a newly bound one).
                "binding_status": "pending" if row_binding is not None else "not_requested",
                "binding_evidence": None,
                "binding_error": None,
                "execution_binding": _binding_evidence_payload(row_binding),
            })
            batch.update({"cases": rows, "status": "dispatching", "updated_at": _now()})
            _atomic_json(path, batch)
            try:
                request = {
                    "title": "aceval %s %s retry" % (purpose, case_id),
                    "prompt": prompt,
                }
                session_id = self.gateway.start_session(request, binding=row_binding) if row_binding is not None else self.gateway.start_session(request)
                if row_binding is not None:
                    evidence = getattr(self.gateway, "last_binding_evidence", None)
                    verify = getattr(self.gateway, "verify_binding", None)
                    if callable(verify):
                        evidence = verify(session_id, row_binding)
                    if evidence is None or (hasattr(evidence, "assert_matches") and not evidence.verified):
                        raise RemoteBatchError("retried session binding was not verified")
                    row.update({"binding_status": "verified", "binding_evidence": _binding_evidence_payload(evidence)})
                row["message_status"] = "sent"
                row.update({"session_id": session_id, "status": "running"})
                self.store.append_event(task_id, "case_run.retried", {"purpose": purpose, "session_id": session_id, "attempt_number": next_attempt, "retry_reason": retry_reason}, iteration=iteration, case_id=case_id, run_id=purpose)
            except Exception as exc:
                row.update({"status": "failed", "error": str(exc), "binding_status": "failed" if row_binding is not None else row.get("binding_status"), "message_status": "failed", "completed_at": _now()})
                _archive_attempt(row)
                self.store.append_event(task_id, "case_run.failed", {"purpose": purpose, "error": str(exc), "retry": True}, iteration=iteration, case_id=case_id, run_id=purpose)
            batch["updated_at"] = _now()
            _atomic_json(path, batch)
        batch["status"] = "running" if any(row.get("status") == "running" for row in rows) else "completed"
        batch["updated_at"] = _now()
        _atomic_json(path, batch)
        self.store.append_event(task_id, "remote.batch_retry_dispatched", {"purpose": purpose, "retried": len(failed), "batch": str(path)}, iteration=iteration)
        return batch

    def wait(self, task_id: str, iteration: int, purpose: str) -> Mapping[str, Any]:
        while True:
            batch = self.collect_once(task_id, iteration, purpose)
            if batch.get("status") == "completed":
                return batch
            time.sleep(self.poll_interval_seconds)


__all__ = ["RemoteBatchCoordinator", "RemoteBatchError", "SessionGateway", "compact_remote_prompt"]
