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
    """Keep the online prompt small; Skill instructions remain in its mounted repository."""

    prompt = str(case.get("prompt") or "").strip()
    if not prompt:
        raise RemoteBatchError("remote case prompt must not be empty")
    standard_text = "\n".join("- " + str(item) for item in standards)
    metadata = case.get("metadata", {}) if isinstance(case.get("metadata"), Mapping) else {}
    repository_lines = []
    repository_checks = []
    for binding in repository_bindings:
        role = str(binding.get("role") or "repository")
        if role not in ("skill", "code"):
            raise RemoteBatchError("repository binding role is invalid")
        branch = str(binding.get("branch") or "").strip()
        if role == "code" and metadata.get("fixture_branch"):
            branch = str(metadata["fixture_branch"]).strip()
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
            "这是新 Skill 的无 Skill 对照运行。请自然完成下面任务，但不得读取或使用挂载仓库中的 SKILL.md、references 或 scripts；"
            "完整保留工具证据，以测量通用 Agent 本身能达到的结果。\n\n"
            "任务：\n%s%s\n\n总体目标：%s\n\n验收标准：\n%s"
            % (prompt, repository_text, goal, standard_text)
        )
    if metadata.get("harness_case_kind") == "negative_trigger":
        return (
            "这是 Skill 触发边界评测。请自然完成下面任务；只有当挂载 Skill 的描述确实适用于任务时才读取并使用它，"
            "不适用时不得读取 SKILL.md，也不得强行套用其流程。完整保留工具证据。\n\n"
            "任务：\n%s%s\n\n总体目标：%s\n\n验收标准：\n%s"
            % (prompt, repository_text, goal, standard_text)
        )
    return (
        "请按当前会话挂载的候选 Skill 严格完成下面任务。先准备下列仓库并从 skill 挂载点读取 SKILL.md（若入口位于 src/SKILL.md 则读取该文件），按其关键步骤执行，"
        "不要修改或发布 Skill 本身。完整保留工具证据。\n\n"
        "任务：\n%s%s\n\n总体目标：%s\n\n验收标准：\n%s"
        % (prompt, repository_text, goal, standard_text)
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
        path = self.batch_path(task_id, iteration, purpose)
        if path.is_file():
            loaded = dict(_load(path))
            if loaded.get("environment_contract_hash") != environment_contract_hash:
                raise RemoteBatchError("persisted batch environment contract does not match requested contract")
            persisted_ids = [str(row.get("case_id") or "") for row in loaded.get("cases", ()) if isinstance(row, Mapping)]
            if any(case_id not in requested_ids for case_id in persisted_ids):
                raise RemoteBatchError("persisted batch does not match requested cases")
            if loaded.get("status") != "dispatching":
                return loaded
            batch = loaded
            case_states = [dict(item) for item in loaded.get("cases", ()) if isinstance(item, Mapping)]
            # The API has no idempotency key. A crash while a session is in
            # `starting` has an unknowable outcome, so fail it closed instead
            # of risking a duplicate remote session.
            for row in case_states:
                if row.get("status") == "starting":
                    row.update({"status": "failed", "error": "dispatch was interrupted before the session id was persisted; duplicate creation was refused", "completed_at": _now()})
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
                "status": "dispatching",
                "created_at": _now(),
                "updated_at": _now(),
                "deadline_at_epoch": time.time() + self.max_wait_seconds,
                "cases": case_states,
            }
            if execution_binding is not None:
                batch["execution_binding"] = _binding_evidence_payload(execution_binding)
            _atomic_json(path, batch)
        # Starting every session before polling gives real fan-out without
        # adding thread-safety assumptions to a company API client.
        persisted_ids = {str(row.get("case_id")) for row in case_states}
        for case_id, prompt, case_title in prepared_cases:
            if case_id in persisted_ids:
                continue
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
                "binding_status": "pending" if execution_binding is not None else "not_requested",
                "binding_evidence": None,
                "message_status": "pending",
                "log_completeness": None,
            }
            case_states.append(row)
            persisted_ids.add(case_id)
            _atomic_json(path, batch)
            try:
                request = {"title": "aceval %s %s" % (purpose, case_id), "prompt": prompt}
                if execution_binding is not None:
                    start = getattr(self.gateway, "start_session", None)
                    if not callable(start):
                        raise RemoteBatchError("session gateway cannot create a bound session")
                    session_id = start(request, binding=execution_binding)
                    evidence = getattr(self.gateway, "last_binding_evidence", None)
                    verify = getattr(self.gateway, "verify_binding", None)
                    if callable(verify):
                        evidence = verify(session_id, execution_binding)
                    if evidence is None:
                        raise RemoteBatchError("session binding was not verified")
                    if hasattr(evidence, "assert_matches"):
                        evidence.assert_matches(execution_binding)
                    row.update({"binding_status": "verified", "binding_evidence": _binding_evidence_payload(evidence)})
                else:
                    session_id = self.gateway.start_session(request)
                row["message_status"] = "sent"
                row.update({"session_id": session_id, "status": "running"})
                self.store.append_event(task_id, "case_run.started", {"purpose": purpose, "session_id": session_id, "binding_status": row.get("binding_status"), "message_status": row.get("message_status")}, iteration=iteration, case_id=case_id, run_id=purpose)
            except Exception as exc:
                row.update({"status": "failed", "error": str(exc), "binding_status": "failed" if execution_binding is not None else row.get("binding_status"), "message_status": "failed", "completed_at": _now()})
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
            artifact = path.parent.parent / "runs" / purpose / _safe_case_file(case_id)
            try:
                bundle = self.gateway.fetch_session(str(row["session_id"]))
                raw_events = None
                fetch_events = getattr(self.gateway, "fetch_events", None)
                if callable(fetch_events):
                    raw_events = [dict(item) for item in fetch_events(str(row["session_id"]))]
                payload = {
                    "api_version": "aceval.kernel-case-run/v1",
                    "task_id": task_id,
                    "iteration": iteration,
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
                if raw_events is not None:
                    payload["events"] = raw_events
                    payload["log_completeness"] = {
                        "raw_events": True,
                        "event_count": len(raw_events),
                        "event_types": sorted({str(item.get("type")) for item in raw_events if isinstance(item, Mapping)}),
                        "required_event_types": ["user.message", "agent.message"],
                        "missing_required_event_types": [kind for kind in ("user.message", "agent.message") if not any(item.get("type") == kind for item in raw_events)],
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
                payload["binding_status"] = row.get("binding_status")
                payload["binding_evidence"] = row.get("binding_evidence")
                _atomic_json(artifact, payload)
                completeness = payload.get("log_completeness") or payload["session"].get("completeness")
                final_error = payload["session"].get("observation", {}).get("error")
                if row.get("binding_status") == "failed":
                    final_error = row.get("binding_error") or "CATX event log did not prove the requested Skill/repository mounts"
                row.update({"status": "completed" if status == "COMPLETED" and row.get("binding_status") != "failed" else "failed", "artifact": str(artifact), "completed_at": _now(), "error": final_error, "log_completeness": completeness})
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

    def retry_failed(self, task_id: str, iteration: int, purpose: str, *, execution_binding: Optional[CatxExecutionBinding] = None) -> Mapping[str, Any]:
        """Recreate only failed sessions while preserving successful evidence."""

        path = self.batch_path(task_id, iteration, purpose)
        batch = dict(_load(path))
        rows = [dict(item) for item in batch.get("cases", ()) if isinstance(item, Mapping)]
        failed = [row for row in rows if row.get("status") == "failed"]
        if not failed:
            raise RemoteBatchError("remote batch has no failed cases to retry")
        for row in failed:
            case_id = str(row.get("case_id") or "")
            prompt = str(row.get("prompt") or "")
            row.update({
                "status": "starting",
                "session_id": None,
                "terminal": None,
                "artifact": None,
                "error": None,
                "fetch_error": None,
                "started_at": _now(),
                "completed_at": None,
            })
            batch.update({"cases": rows, "status": "dispatching", "updated_at": _now()})
            _atomic_json(path, batch)
            try:
                request = {
                    "title": "aceval %s %s retry" % (purpose, case_id),
                    "prompt": prompt,
                }
                session_id = self.gateway.start_session(request, binding=execution_binding) if execution_binding is not None else self.gateway.start_session(request)
                if execution_binding is not None:
                    evidence = getattr(self.gateway, "last_binding_evidence", None)
                    verify = getattr(self.gateway, "verify_binding", None)
                    if callable(verify):
                        evidence = verify(session_id, execution_binding)
                    if evidence is None or (hasattr(evidence, "assert_matches") and not evidence.verified):
                        raise RemoteBatchError("retried session binding was not verified")
                    row.update({"binding_status": "verified", "binding_evidence": _binding_evidence_payload(evidence)})
                    row["message_status"] = "sent"
                row.update({"session_id": session_id, "status": "running"})
                self.store.append_event(task_id, "case_run.retried", {"purpose": purpose, "session_id": session_id}, iteration=iteration, case_id=case_id, run_id=purpose)
            except Exception as exc:
                row.update({"status": "failed", "error": str(exc), "binding_status": "failed" if execution_binding is not None else row.get("binding_status"), "message_status": "failed", "completed_at": _now()})
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
