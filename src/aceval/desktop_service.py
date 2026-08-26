"""Private JSON-RPC bridge between the Electron shell and the Python Kernel.

The service uses newline-delimited JSON over inherited stdio.  It never opens a
network port, never evaluates arbitrary shell text, and keeps long Kernel/D2C
operations in bounded background workers so the renderer can poll persisted
task events while work is in progress.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable, Mapping, Optional
import uuid

from . import __version__
from .d2c import (
    D2C_PROFILE_API_VERSION,
    D2CBrowserProfile,
    D2CValidationRequest,
    check_d2c_profile,
    validate_d2c,
)
from .iteration_kernel import IterationKernel
from .kernel_contracts import KernelConfig, KernelInput
from .task_center import TaskStore


DESKTOP_SERVICE_API_VERSION = "aceval.desktop-service/v1"


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_profile() -> Mapping[str, Any]:
    node = os.environ.get("ACEVAL_D2C_NODE_EXECUTABLE") or shutil.which("node") or "/usr/local/bin/node"
    chrome_candidates = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
    )
    chrome = next((item for item in chrome_candidates if item and Path(item).is_file()), chrome_candidates[0])
    return {
        "api_version": D2C_PROFILE_API_VERSION,
        "name": "local-chrome-1440",
        "node_executable": node,
        "chrome_executable": chrome,
        "viewport_width": 1440,
        "viewport_height": 900,
        "device_scale_factor": 1.0,
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
        "color_scheme": "light",
        "stability_wait_ms": 800,
        "timeout_seconds": 90,
    }


def _command_health(name: str, *version_args: str) -> Mapping[str, Any]:
    executable = shutil.which(name)
    if not executable:
        return {"ready": False, "executable": None, "version": None, "error": "%s was not found" % name}
    try:
        result = subprocess.run(
            (executable,) + version_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        version = (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
        return {
            "ready": result.returncode == 0,
            "executable": executable,
            "version": version,
            "error": None if result.returncode == 0 else "exit status %d" % result.returncode,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ready": False, "executable": executable, "version": None, "error": str(exc)}


class DesktopService:
    def __init__(self, task_root: Path) -> None:
        self.task_root = task_root.expanduser().resolve()
        self.task_root.mkdir(parents=True, exist_ok=True)
        self.kernel = IterationKernel(self.task_root)
        self.store = TaskStore(self.task_root)
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="aceval-desktop")
        self.operations: dict[str, dict[str, Any]] = {}
        self.task_operations: dict[str, str] = {}
        self.lock = threading.RLock()

    def _start_operation(self, kind: str, action: Callable[[], Any], *, task_id: Optional[str] = None) -> Mapping[str, Any]:
        with self.lock:
            if task_id and task_id in self.task_operations:
                operation = self.operations.get(self.task_operations[task_id])
                if operation and operation.get("status") == "running":
                    raise ValueError("task already has a running desktop operation")
            operation_id = "op_" + uuid.uuid4().hex
            operation = {
                "id": operation_id,
                "kind": kind,
                "task_id": task_id,
                "status": "running",
                "created_at": _now(),
                "updated_at": _now(),
                "result": None,
                "error": None,
            }
            self.operations[operation_id] = operation
            if task_id:
                self.task_operations[task_id] = operation_id

        def work() -> None:
            try:
                result = _json_clone(action())
                with self.lock:
                    operation.update({"status": "completed", "result": result, "updated_at": _now()})
            except Exception as exc:
                with self.lock:
                    operation.update({"status": "failed", "error": str(exc), "updated_at": _now()})
            finally:
                with self.lock:
                    if task_id and self.task_operations.get(task_id) == operation_id:
                        self.task_operations.pop(task_id, None)

        self.executor.submit(work)
        return dict(operation)

    def _task_list(self) -> list[Mapping[str, Any]]:
        result = []
        for item in self.store.list():
            state_path = self.store.task_dir(str(item["id"])) / "kernel" / "state.json"
            # The shared TaskStore may contain legacy report-only tasks.  They
            # are intentionally excluded from this stateful product client;
            # attempting to open one as a Kernel task would create a broken
            # first-run experience.
            if not state_path.is_file():
                continue
            try:
                state = self.kernel.state(str(item["id"]))
            except Exception:
                continue
            row = dict(item)
            row.update({
                "phase": state.get("phase"),
                "candidate_status": state.get("candidate_status"),
                "champion_commit": state.get("champion_commit"),
            })
            with self.lock:
                operation_id = self.task_operations.get(str(item["id"]))
                row["active_operation"] = dict(self.operations[operation_id]) if operation_id in self.operations else None
            result.append(row)
        return result

    def handle(self, method: str, params: Mapping[str, Any]) -> Any:
        if method == "system.bootstrap":
            profile = D2CBrowserProfile.from_mapping(_default_profile())
            d2c_health = check_d2c_profile(profile)
            return {
                "api_version": DESKTOP_SERVICE_API_VERSION,
                "version": __version__,
                "task_root": str(self.task_root),
                "d2c_profile": profile.as_dict(),
                "d2c_health": d2c_health,
                "environment_health": {
                    "kernel": {
                        "ready": True,
                        "version": __version__,
                        "python": sys.version.split()[0],
                        "bundled": bool(getattr(sys, "frozen", False)),
                    },
                    "git": _command_health("git", "--version"),
                    "d2c": {
                        "ready": d2c_health["ready"],
                        "node": d2c_health.get("versions", {}).get("node"),
                        "chrome": d2c_health.get("versions", {}).get("chrome"),
                        "missing": d2c_health.get("missing", []),
                    },
                },
                "capabilities": {
                    "skill_harness": True,
                    "agent_subject_contract": True,
                    "d2c_browser_worker": True,
                    "visual_comparison": True,
                    "remote_agent": True,
                },
            }
        if method == "tasks.list":
            return {"tasks": self._task_list()}
        if method == "tasks.get":
            return self.kernel.snapshot(str(params.get("task_id") or ""))
        if method == "tasks.events":
            return {
                "events": list(self.store.events(str(params.get("task_id") or ""), after_seq=int(params.get("after_seq", 0))))
            }
        if method == "tasks.create":
            config = KernelConfig.from_mapping(params.get("config"))
            user_input = KernelInput.from_mapping(params.get("input"))
            task_id = params.get("task_id")
            return self.kernel.create_task(config, user_input, task_id=str(task_id) if task_id else None)
        if method == "tasks.run":
            task_id = str(params.get("task_id") or "")
            max_steps = int(params.get("max_steps", 200))
            return self._start_operation(
                "kernel.run_until_gate",
                lambda: self.kernel.run_until_gate(task_id, max_steps=max_steps),
                task_id=task_id,
            )
        if method == "tasks.confirm":
            task_id = str(params.get("task_id") or "")
            supplement = KernelInput.from_mapping(params["supplement"]) if params.get("supplement") is not None else None
            result = self.kernel.confirm(
                task_id,
                approve=bool(params.get("approve")),
                supplement=supplement,
                selected_capability_ids=tuple(str(item) for item in params.get("selected_capability_ids", ())),
                selected_change_ids=tuple(str(item) for item in params.get("selected_change_ids", ())),
                user_feedback=str(params["user_feedback"]) if params.get("user_feedback") is not None else None,
            )
            if params.get("continue"):
                return {
                    "confirmation": result,
                    "operation": self._start_operation(
                        "kernel.run_until_gate",
                        lambda: self.kernel.run_until_gate(task_id),
                        task_id=task_id,
                    ),
                }
            return result
        if method == "tasks.log":
            return self.kernel.session_log(
                str(params.get("task_id") or ""),
                int(params.get("iteration", 0)),
                str(params.get("purpose") or ""),
                str(params.get("case_id") or ""),
            )
        if method == "operations.get":
            operation_id = str(params.get("operation_id") or "")
            with self.lock:
                if operation_id not in self.operations:
                    raise ValueError("desktop operation does not exist")
                return dict(self.operations[operation_id])
        if method == "d2c.check":
            return check_d2c_profile(D2CBrowserProfile.from_mapping(params.get("profile") or _default_profile()))
        if method == "d2c.validate":
            task_id = str(params.get("task_id") or "")
            profile = D2CBrowserProfile.from_mapping(params.get("profile") or _default_profile())
            request = D2CValidationRequest.from_mapping(params.get("request"))
            iteration = int(params.get("iteration", 0))
            case_slug = "".join(item if item.isalnum() or item in "-_" else "-" for item in request.case_id)[:80]
            output = self.store.task_dir(task_id) / "iterations" / ("iteration-%03d" % iteration) / "d2c" / (case_slug + "-" + uuid.uuid4().hex[:10])

            def run_d2c() -> Any:
                self.store.append_event(task_id, "case_run.started", {"provider": "local-browser", "url": request.url}, iteration=iteration, case_id=request.case_id, run_id="browser-1")
                try:
                    receipt = validate_d2c(profile, request, output)
                except Exception as exc:
                    self.store.append_event(task_id, "case_run.failed", {"provider": "local-browser", "error": str(exc)}, iteration=iteration, case_id=request.case_id, run_id="browser-1")
                    raise
                self.store.append_event(task_id, "case_run.completed", {"provider": "local-browser", "status": receipt["status"], "receipt": str(output / "receipt.json"), "artifacts": receipt["artifacts"]}, iteration=iteration, case_id=request.case_id, run_id="browser-1")
                return receipt

            return self._start_operation("d2c.validate", run_d2c, task_id=task_id)
        raise ValueError("unsupported desktop method: %s" % method)


def serve(task_root: Path) -> int:
    service = DesktopService(task_root)
    write_lock = threading.Lock()

    def respond(value: Mapping[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with write_lock:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()

    respond({"type": "ready", "api_version": DESKTOP_SERVICE_API_VERSION, "pid": os.getpid()})
    for line in sys.stdin:
        request: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise ValueError("desktop request must be an object")
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(request_id, str) or not request_id or not isinstance(method, str) or not isinstance(params, Mapping):
                raise ValueError("desktop request envelope is invalid")
            respond({"id": request_id, "ok": True, "result": service.handle(method, params)})
        except Exception as exc:
            respond({"id": request.get("id") if isinstance(request, Mapping) else None, "ok": False, "error": {"message": str(exc), "type": type(exc).__name__}})
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aceval.desktop_service")
    parser.add_argument("--task-root", default=".aceval/tasks")
    args = parser.parse_args(argv)
    try:
        return serve(Path(args.task_root))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
