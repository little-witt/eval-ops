"""Filesystem-backed task center and append-only optimization event stream."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple, Union
import uuid

from .environment_contracts import canonical_hash


TASK_API_VERSION = "aceval.evaluation-task/v1"
TASK_INDEX_API_VERSION = "aceval.task-index/v1"
TASK_EVENT_API_VERSION = "aceval.task-event/v1"
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")


class TaskCenterError(ValueError):
    """Task state is malformed, missing, or unsafe to persist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum or "\x00" in value:
        raise TaskCenterError("%s must be a trimmed non-empty string" % label)
    return value


def _json_clone(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise TaskCenterError("%s must be strict JSON" % label) from exc


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    temporary.write_text(payload, encoding="utf-8")
    os.replace(str(temporary), str(path))


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskCenterError("cannot read %s" % label) from exc
    if not isinstance(value, Mapping):
        raise TaskCenterError("%s must be a JSON object" % label)
    return value


def _task_id(value: str) -> str:
    if not TASK_ID_PATTERN.fullmatch(value):
        raise TaskCenterError("task id must match %s" % TASK_ID_PATTERN.pattern)
    return value


class TaskStore:
    """Owns task metadata and a per-task, ordered JSONL event log."""

    def __init__(self, root: Union[str, Path] = ".aceval/tasks", *, create: bool = True) -> None:
        self.root = Path(root).expanduser().resolve()
        if create:
            self.root.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        return self.root / _task_id(task_id)

    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _write_index(self) -> None:
        tasks = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not TASK_ID_PATTERN.fullmatch(directory.name):
                continue
            document = _load_json(directory / "task.json", "task")
            tasks.append({
                "id": document.get("id"),
                "skill_name": document.get("skill", {}).get("name") if isinstance(document.get("skill"), Mapping) else None,
                "scenario": document.get("scenario"),
                "status": document.get("status"),
                "created_at": document.get("created_at"),
                "updated_at": document.get("updated_at"),
                "current_iteration": document.get("current_iteration", 0),
            })
        tasks.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        _atomic_json(self._index_path(), {"api_version": TASK_INDEX_API_VERSION, "tasks": tasks})

    def create(
        self,
        *,
        skill_name: str,
        skill_source: str,
        scenario: str,
        goal: str,
        standards: Sequence[str],
        cases: Sequence[Mapping[str, Any]] = (),
        evaluation_spec: Optional[Mapping[str, Any]] = None,
        task_id: Optional[str] = None,
        environment: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        skill_name = _text(skill_name, "skill_name", 256)
        skill_source = _text(skill_source, "skill_source")
        scenario = _text(scenario, "scenario", 128)
        goal = _text(goal, "goal", 8192)
        normalized_standards = [_text(item, "standards[]", 2048) for item in standards]
        if not normalized_standards:
            raise TaskCenterError("standards must not be empty")
        if task_id is None:
            slug = re.sub(r"[^a-z0-9]+", "-", skill_name.lower()).strip("-")[:48] or "skill"
            task_id = "%s-%s" % (slug, uuid.uuid4().hex[:10])
        task_id = _task_id(task_id)
        directory = self.task_dir(task_id)
        if directory.exists():
            raise TaskCenterError("task already exists: %s" % task_id)
        directory.mkdir(parents=True)
        now = _now()
        normalized_cases = [_json_clone(item, "cases[]") for item in cases]
        if evaluation_spec is None:
            evaluation = {
                "mode": "automatic",
                "status": "draft",
                "cases": normalized_cases,
                "standards": normalized_standards,
                "reused_from": None,
                "customizable": True,
            }
            design_event = "evaluation.design_generated"
        else:
            evaluation = _json_clone(evaluation_spec, "evaluation_spec")
            evaluation.setdefault("mode", "custom")
            evaluation.setdefault("customizable", True)
            evaluation.setdefault("cases", normalized_cases)
            evaluation.setdefault("standards", normalized_standards)
            design_event = "evaluation.design_attached"
        task = {
            "api_version": TASK_API_VERSION,
            "id": task_id,
            "skill": {"name": skill_name, "source": skill_source},
            "scenario": scenario,
            "goal": goal,
            "standards": normalized_standards,
            "evaluation": evaluation,
            "environment": _json_clone(environment or {}, "environment"),
            "status": "ready",
            "current_iteration": 0,
            "created_at": now,
            "updated_at": now,
        }
        task["contract_hash"] = canonical_hash(task)
        _atomic_json(directory / "task.json", task)
        self.append_event(task_id, "task.created", {"task": task})
        self.append_event(task_id, design_event, {"evaluation": evaluation})
        self._write_index()
        return task

    def load(self, task_id: str) -> Mapping[str, Any]:
        task = _load_json(self.task_dir(task_id) / "task.json", "task")
        if task.get("api_version") != TASK_API_VERSION or task.get("id") != task_id:
            raise TaskCenterError("task contract does not match requested id")
        return task

    def list(self) -> Tuple[Mapping[str, Any], ...]:
        if not self.root.is_dir():
            return ()
        tasks = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not TASK_ID_PATTERN.fullmatch(directory.name):
                continue
            document = _load_json(directory / "task.json", "task")
            tasks.append({
                "id": document.get("id"),
                "skill_name": document.get("skill", {}).get("name") if isinstance(document.get("skill"), Mapping) else None,
                "scenario": document.get("scenario"),
                "status": document.get("status"),
                "created_at": document.get("created_at"),
                "updated_at": document.get("updated_at"),
                "current_iteration": document.get("current_iteration", 0),
            })
        tasks.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return tuple(tasks)

    def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        iteration: Optional[int] = None,
        case_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        event_type = _text(event_type, "event_type", 256)
        directory = self.task_dir(task_id)
        if not (directory / "task.json").is_file():
            raise TaskCenterError("task does not exist: %s" % task_id)
        event_path = directory / "events.jsonl"
        event_path.touch(exist_ok=True)
        with event_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            lines = [line for line in handle.read().splitlines() if line.strip()]
            sequence = len(lines) + 1
            event = {
                "api_version": TASK_EVENT_API_VERSION,
                "event_id": "%s:%08d" % (task_id, sequence),
                "task_id": task_id,
                "seq": sequence,
                "object_seq": sequence,
                "previous_event_id": "%s:%08d" % (task_id, sequence - 1) if sequence > 1 else None,
                "timestamp": _now(),
                "type": event_type,
                "iteration": iteration,
                "case_id": case_id,
                "run_id": run_id,
                "payload": _json_clone(payload or {}, "event payload"),
            }
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return event

    def events(self, task_id: str, *, after_seq: int = 0) -> Tuple[Mapping[str, Any], ...]:
        path = self.task_dir(task_id) / "events.jsonl"
        if not path.is_file():
            return ()
        result = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TaskCenterError("invalid task event at line %d" % line_number) from exc
            if not isinstance(event, Mapping) or event.get("api_version") != TASK_EVENT_API_VERSION:
                raise TaskCenterError("invalid task event at line %d" % line_number)
            if int(event.get("seq", 0)) > after_seq:
                result.append(event)
        return tuple(result)

    def update(self, task_id: str, **changes: Any) -> Mapping[str, Any]:
        allowed = {"status", "current_iteration", "environment", "evaluation"}
        unknown = sorted(set(changes).difference(allowed))
        if unknown:
            raise TaskCenterError("unsupported task updates: %s" % ", ".join(unknown))
        task = dict(self.load(task_id))
        task.update(_json_clone(changes, "task updates"))
        task["updated_at"] = _now()
        task.pop("contract_hash", None)
        task["contract_hash"] = canonical_hash(task)
        _atomic_json(self.task_dir(task_id) / "task.json", task)
        self._write_index()
        return task

    def begin_iteration(
        self,
        task_id: str,
        *,
        hypothesis: str,
        planned_changes: Sequence[str],
        target_dimensions: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        task = self.load(task_id)
        iteration = int(task.get("current_iteration", 0)) + 1
        self.update(task_id, status="running", current_iteration=iteration)
        return self.append_event(
            task_id,
            "iteration.planned",
            {
                "hypothesis": _text(hypothesis, "hypothesis", 8192),
                "planned_changes": [_text(item, "planned_changes[]", 2048) for item in planned_changes],
                "target_dimensions": [_text(item, "target_dimensions[]", 1024) for item in target_dimensions],
            },
            iteration=iteration,
        )

    def record_decision(
        self,
        task_id: str,
        *,
        iteration: int,
        decision: str,
        reason: str,
        metrics: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        if decision not in ("promote", "reject", "continue", "converged", "blocked"):
            raise TaskCenterError("unsupported iteration decision")
        event = self.append_event(
            task_id,
            "decision.recorded",
            {"decision": decision, "reason": _text(reason, "reason", 8192), "metrics": _json_clone(metrics or {}, "metrics")},
            iteration=iteration,
        )
        status = "converged" if decision == "converged" else ("blocked" if decision == "blocked" else "ready")
        current = int(self.load(task_id).get("current_iteration", 0))
        self.update(task_id, status=status, current_iteration=max(iteration, current))
        if decision in ("converged", "blocked"):
            self.append_event(task_id, "task.%s" % decision, {"reason": reason}, iteration=iteration)
        return event


__all__ = ["TASK_API_VERSION", "TASK_EVENT_API_VERSION", "TASK_INDEX_API_VERSION", "TaskCenterError", "TaskStore"]
