"""Local read-only web console for optimization workspaces."""

from __future__ import annotations

import json
import re
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple, Type, Union
from urllib.parse import urlsplit

from .optimization_graph import compile_optimization_graph, write_optimization_graph
from .iteration_kernel import IterationKernel
from .task_center import TASK_ID_PATTERN, TaskCenterError, TaskStore


CONSOLE_API_VERSION = "aceval.console/v1"
LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))
ASSET_CONTENT_TYPES = {
    "/": "text/html; charset=utf-8",
    "/index.html": "text/html; charset=utf-8",
    "/app.js": "text/javascript; charset=utf-8",
    "/styles.css": "text/css; charset=utf-8",
    "/optimization-graph.js": "text/javascript; charset=utf-8",
    "/task-center.js": "text/javascript; charset=utf-8",
}
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_LOG_PURPOSES = frozenset(("evaluation", "pass-verification", "without-skill-baseline"))


class ConsoleError(ValueError):
    """Raised when the local console would violate its read-only boundary."""


def _asset_root() -> Path:
    return Path(__file__).resolve().parent / "console_assets"


def _task_entry(store: TaskStore, task_id: str, kernel: Optional[IterationKernel]) -> Mapping[str, Any]:
    entry: dict[str, Any] = {"task": store.load(task_id), "events": list(store.events(task_id))}
    if kernel is not None and (store.task_dir(task_id) / "kernel" / "state.json").is_file():
        try:
            entry["kernel"] = kernel.snapshot(task_id)
        except Exception as exc:
            entry["kernel_error"] = str(exc)
    return entry


def build_console(
    workspace: Union[str, Path],
    output: Union[str, Path],
    *,
    plan_path: Optional[Union[str, Path]] = None,
    profile_path: Optional[Union[str, Path]] = None,
    task_root: Optional[Union[str, Path]] = None,
) -> Mapping[str, str]:
    target = Path(output).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    assets = _asset_root()
    for name in ("index.html", "app.js", "styles.css"):
        source = assets / name
        if not source.is_file():
            raise ConsoleError("console asset is missing: %s" % source)
        shutil.copyfile(source, target / name)
    graph = write_optimization_graph(
        workspace,
        target / "optimization-graph.json",
        plan_path=plan_path,
        profile_path=profile_path,
    )
    graph_value = json.loads(graph.read_text(encoding="utf-8"))
    (target / "optimization-graph.js").write_text(
        "window.__ACEVAL_GRAPH__ = "
        + json.dumps(graph_value, ensure_ascii=False, allow_nan=False)
        + ";\n",
        encoding="utf-8",
    )
    tasks = []
    if task_root:
        store = TaskStore(task_root, create=False)
        kernel = IterationKernel(task_root)
        for summary in store.list():
            task_id = str(summary["id"])
            entry = dict(_task_entry(store, task_id, kernel))
            entry["summary"] = summary
            tasks.append(entry)
    (target / "task-center.js").write_text(
        "window.__ACEVAL_TASKS__ = "
        + json.dumps(tasks, ensure_ascii=False, allow_nan=False)
        + ";\n",
        encoding="utf-8",
    )
    return {
        "api_version": CONSOLE_API_VERSION,
        "mode": "static",
        "index": str((target / "index.html").resolve()),
        "graph": str(graph),
    }


def _workspace_fingerprint(workspace: Optional[Path], plan: Optional[Path], task_root: Optional[Path] = None) -> Tuple[int, int, int]:
    latest_ns = 0
    total_size = 0
    count = 0
    candidates = [workspace / "history.json"] if workspace else []
    if plan:
        candidates.append(plan)
    if workspace:
        candidates.extend(workspace.glob("iteration-*/*/*.json"))
        candidates.extend(workspace.glob("iteration-*/eval-*/*/run-1/*.json"))
    if task_root and task_root.is_dir():
        candidates.extend(task_root.glob("*/task.json"))
        candidates.extend(task_root.glob("*/events.jsonl"))
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        latest_ns = max(latest_ns, stat.st_mtime_ns)
        total_size += stat.st_size
        count += 1
    return latest_ns, total_size, count


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def create_console_handler(
    workspace: Optional[Union[str, Path]],
    *,
    plan_path: Optional[Union[str, Path]] = None,
    profile_path: Optional[Union[str, Path]] = None,
    task_root: Optional[Union[str, Path]] = None,
) -> Type[BaseHTTPRequestHandler]:
    root = Path(workspace).expanduser().resolve() if workspace else None
    plan = Path(plan_path).expanduser().resolve() if plan_path else None
    profile = Path(profile_path).expanduser().resolve() if profile_path else None
    assets = _asset_root()
    tasks_path = Path(task_root).expanduser().resolve() if task_root else None
    task_store = TaskStore(tasks_path, create=False) if tasks_path else None
    kernel_reader = IterationKernel(tasks_path) if tasks_path else None

    class ConsoleHandler(BaseHTTPRequestHandler):
        server_version = "aceval-console/1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _headers(self, status: int, content_type: str, length: Optional[int] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
            )
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def _send_json(self, status: int, value: Any) -> None:
            payload = _json_bytes(value)
            self._headers(status, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)

        def _graph(self) -> Mapping[str, Any]:
            if root is None:
                raise ConsoleError("no optimization workspace is selected")
            return compile_optimization_graph(
                root, plan_path=plan, profile_path=profile
            )

        def _events(self) -> None:
            self._headers(200, "text/event-stream; charset=utf-8")
            last_fingerprint = None
            last_valid: Optional[Mapping[str, Any]] = None
            heartbeat_at = 0.0
            deadline = time.monotonic() + 1800.0
            try:
                while time.monotonic() < deadline:
                    fingerprint = _workspace_fingerprint(root, plan, tasks_path)
                    if fingerprint != last_fingerprint:
                        try:
                            last_valid = self._graph()
                        except Exception as exc:
                            if last_valid is None:
                                error = {"message": str(exc), "recoverable": True}
                                self.wfile.write(b"event: graph.error\n")
                                self.wfile.write(b"data: " + _json_bytes(error) + b"\n\n")
                                self.wfile.flush()
                        else:
                            self.wfile.write(b"event: graph.snapshot\n")
                            self.wfile.write(b"data: " + _json_bytes(last_valid) + b"\n\n")
                            self.wfile.flush()
                        if task_store is not None:
                            task_snapshot = []
                            for summary in task_store.list():
                                task_id = str(summary["id"])
                                entry = dict(_task_entry(task_store, task_id, kernel_reader))
                                entry["summary"] = summary
                                task_snapshot.append(entry)
                            self.wfile.write(b"event: task.snapshot\n")
                            self.wfile.write(b"data: " + _json_bytes(task_snapshot) + b"\n\n")
                            self.wfile.flush()
                        last_fingerprint = fingerprint
                    now = time.monotonic()
                    if now >= heartbeat_at:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        heartbeat_at = now + 15.0
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError):
                return

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            parsed = urlsplit(self.path)
            route = parsed.path
            if route == "/api/health":
                self._send_json(200, {"ok": True, "api_version": CONSOLE_API_VERSION})
                return
            if route == "/api/graph":
                try:
                    self._send_json(200, self._graph())
                except Exception as exc:
                    self._send_json(422, {"error": str(exc)})
                return
            if route == "/api/tasks":
                if task_store is None:
                    self._send_json(200, {"tasks": []})
                    return
                self._send_json(200, {"tasks": task_store.list()})
                return
            if route.startswith("/api/tasks/"):
                parts = route.strip("/").split("/")
                if len(parts) == 7 and parts[:2] == ["api", "tasks"] and parts[3] == "logs":
                    task_id, iteration_text, purpose, case_id = parts[2], parts[4], parts[5], parts[6]
                    if (
                        kernel_reader is None
                        or not TASK_ID_PATTERN.fullmatch(task_id)
                        or not iteration_text.isdigit()
                        or purpose not in _LOG_PURPOSES
                        or not _CASE_ID.fullmatch(case_id)
                    ):
                        self._send_json(404, {"error": "session log not found"})
                        return
                    try:
                        self._send_json(200, kernel_reader.session_log(task_id, int(iteration_text), purpose, case_id))
                    except Exception as exc:
                        self._send_json(404, {"error": str(exc)})
                    return
                task_id = route[len("/api/tasks/"):]
                if task_store is None or not TASK_ID_PATTERN.fullmatch(task_id):
                    self._send_json(404, {"error": "task not found"})
                    return
                try:
                    self._send_json(200, _task_entry(task_store, task_id, kernel_reader))
                except TaskCenterError as exc:
                    self._send_json(404, {"error": str(exc)})
                return
            if route == "/api/events":
                self._events()
                return
            if route == "/optimization-graph.js":
                try:
                    graph_value: Any = self._graph()
                except Exception:
                    graph_value = None
                payload = ("window.__ACEVAL_GRAPH__ = " + json.dumps(graph_value, ensure_ascii=False, allow_nan=False) + ";\n").encode("utf-8")
                self._headers(200, ASSET_CONTENT_TYPES[route], len(payload))
                self.wfile.write(payload)
                return
            if route == "/task-center.js":
                values = []
                if task_store is not None:
                    for summary in task_store.list():
                        task_id = str(summary["id"])
                        entry = dict(_task_entry(task_store, task_id, kernel_reader))
                        entry["summary"] = summary
                        values.append(entry)
                payload = ("window.__ACEVAL_TASKS__ = " + json.dumps(values, ensure_ascii=False, allow_nan=False) + ";\n").encode("utf-8")
                self._headers(200, ASSET_CONTENT_TYPES[route], len(payload))
                self.wfile.write(payload)
                return
            if route not in ASSET_CONTENT_TYPES:
                self._send_json(404, {"error": "not found"})
                return
            name = "index.html" if route in ("/", "/index.html") else route.lstrip("/")
            asset = assets / name
            try:
                payload = asset.read_bytes()
            except OSError:
                self._send_json(404, {"error": "asset not found"})
                return
            self._headers(200, ASSET_CONTENT_TYPES[route], len(payload))
            self.wfile.write(payload)

    return ConsoleHandler


def serve_console(
    workspace: Optional[Union[str, Path]],
    *,
    plan_path: Optional[Union[str, Path]] = None,
    profile_path: Optional[Union[str, Path]] = None,
    task_root: Optional[Union[str, Path]] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ConsoleError("console host must be loopback-only")
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise ConsoleError("console port must be between 1 and 65535")
    handler = create_console_handler(
        workspace, plan_path=plan_path, profile_path=profile_path, task_root=task_root
    )
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


__all__ = [
    "CONSOLE_API_VERSION",
    "ConsoleError",
    "build_console",
    "create_console_handler",
    "serve_console",
]
