"""Isolated Codex profiles and the Codex App Server inference adapter.

The desktop product never points Codex at the user's live ``CODEX_HOME``.
Selected model configuration and authentication files are validated, copied
into an app-owned profile, and used through the official App Server JSONL
protocol.  Raw credentials are never returned by this module.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9/3.10
    import tomli as tomllib  # type: ignore[no-redef]


PROFILE_API_VERSION = "aceval.codex-profile/v1"
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MAX_IMPORT_BYTES = 1024 * 1024
_PROBE_BOUNDARY_FORMAT_CHARS = "\u200b\u200c\u200d\u2060\ufeff"
_MODEL_TOP_LEVEL_KEYS = (
    "model",
    "model_provider",
    "review_model",
    "model_reasoning_effort",
    "disable_response_storage",
    "network_access",
)
_MODEL_PROVIDER_KEYS = (
    "name",
    "base_url",
    "wire_api",
    "requires_openai_auth",
    "env_key",
    "env_key_instructions",
    "request_max_retries",
    "stream_max_retries",
    "stream_idle_timeout_ms",
    "supports_websockets",
)
_SAFE_CHILD_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "LANG",
    "LC_ALL",
    "TZ",
)


class CodexProfileError(RuntimeError):
    """A safe, user-displayable Codex profile or App Server error."""


class CodexDirectUnavailable(CodexProfileError):
    """The profile must use Codex App Server instead of direct Responses."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _absolute_path_preserving_symlinks(value: Path | str) -> Path:
    """Return an absolute lexical path without replacing an NVM launcher symlink.

    NVM keeps ``bin/codex`` beside the matching ``node`` executable. Resolving
    that link to the package's JavaScript entrypoint loses the runtime pairing
    and breaks Finder-launched desktop apps whose PATH does not contain NVM.
    """
    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _profile_id(value: Any) -> str:
    text = str(value or "")
    if not _PROFILE_ID.fullmatch(text):
        raise CodexProfileError("Codex profile id is invalid")
    return text


def _read_regular_file(source: Path) -> bytes:
    candidate = source.expanduser()
    if candidate.is_symlink():
        raise CodexProfileError("Codex import source must not be a symbolic link")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(candidate), flags)
    except OSError as exc:
        raise CodexProfileError("Codex import source cannot be opened") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise CodexProfileError("Codex import source must be a regular file")
        if details.st_size <= 0 or details.st_size > _MAX_IMPORT_BYTES:
            raise CodexProfileError("Codex import source has an invalid size")
        chunks = []
        remaining = _MAX_IMPORT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > _MAX_IMPORT_BYTES:
            raise CodexProfileError("Codex import source exceeds the size limit")
        return value
    finally:
        os.close(descriptor)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and "\x00" not in value:
        return json.dumps(value, ensure_ascii=False)
    raise CodexProfileError("Codex config contains an unsupported model setting")


def _sanitize_config(raw: bytes) -> bytes:
    try:
        text = raw.decode("utf-8")
        value = tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CodexProfileError("Codex config.toml is not valid TOML") from exc
    if not isinstance(value, Mapping):
        raise CodexProfileError("Codex config.toml must contain a TOML object")

    lines = [
        "# Imported by FORGE. Only model connectivity settings are retained.",
        "# The source Codex configuration is never modified.",
    ]
    for key in _MODEL_TOP_LEVEL_KEYS:
        if key in value:
            lines.append("%s = %s" % (key, _toml_scalar(value[key])))
    providers = value.get("model_providers", {})
    if providers is not None and not isinstance(providers, Mapping):
        raise CodexProfileError("Codex model_providers must be a TOML table")
    for provider_id, provider in sorted((providers or {}).items()):
        if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", provider_id):
            raise CodexProfileError("Codex model provider id is invalid")
        if not isinstance(provider, Mapping):
            raise CodexProfileError("Codex model provider must be a TOML table")
        retained = [(key, provider[key]) for key in _MODEL_PROVIDER_KEYS if key in provider]
        # CC Switch uses this non-secret sentinel to route Codex through its
        # local streaming proxy. Preserve only the exact sentinel on loopback;
        # never copy arbitrary bearer tokens from config.toml.
        proxy_token = provider.get("experimental_bearer_token")
        base_url = provider.get("base_url")
        if (
            proxy_token == "PROXY_MANAGED"
            and isinstance(base_url, str)
            and urlparse(base_url).scheme == "http"
            and urlparse(base_url).hostname in ("127.0.0.1", "localhost", "::1")
        ):
            retained.append(("experimental_bearer_token", proxy_token))
        if not retained:
            continue
        lines.extend(("", "[model_providers.%s]" % provider_id))
        lines.extend("%s = %s" % (key, _toml_scalar(item)) for key, item in retained)
    if len(lines) == 2:
        raise CodexProfileError("Codex config.toml has no supported model settings")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _is_managed_loopback_config(raw: bytes) -> bool:
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        return False
    providers = value.get("model_providers") if isinstance(value, Mapping) else None
    if not isinstance(providers, Mapping):
        return False
    for provider in providers.values():
        if not isinstance(provider, Mapping) or provider.get("experimental_bearer_token") != "PROXY_MANAGED":
            continue
        parsed = urlparse(str(provider.get("base_url") or ""))
        if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
            return True
    return False


def _validate_auth(raw: bytes) -> bytes:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CodexProfileError("Codex auth.json is not valid JSON") from exc
    if not isinstance(value, Mapping) or not value:
        raise CodexProfileError("Codex auth.json must contain an authentication object")
    # Codex has used both API-key and ChatGPT token records.  Keep the record
    # opaque while requiring at least one non-empty credential-shaped field.
    candidates = []
    for key in ("OPENAI_API_KEY", "tokens", "access_token", "refresh_token"):
        item = value.get(key)
        if isinstance(item, str):
            candidates.append(bool(item.strip()))
        elif isinstance(item, Mapping):
            candidates.append(any(isinstance(part, str) and bool(part.strip()) for part in item.values()))
    if not any(candidates):
        raise CodexProfileError("Codex auth.json does not contain a supported credential")
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CodexProfileError("Codex auth.json contains unsupported values") from exc


def _atomic_private_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".forge-import-", dir=str(target.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _safe_environment(codex_home: Path, codex_executable: Path) -> Mapping[str, str]:
    environment = {name: os.environ[name] for name in _SAFE_CHILD_ENV if name in os.environ}
    original_path = os.environ.get("PATH", os.defpath)
    environment["PATH"] = str(codex_executable.parent) + os.pathsep + original_path
    environment["HOME"] = str(Path.home())
    environment["CODEX_HOME"] = str(codex_home)
    environment.setdefault("LANG", "en_US.UTF-8")
    return environment


class CodexAppServer:
    """Small synchronous client for one isolated Codex App Server process."""

    def __init__(self, codex_executable: Path, codex_home: Path, timeout_seconds: float = 180) -> None:
        self.codex_executable = _absolute_path_preserving_symlinks(codex_executable)
        self.codex_home = codex_home.expanduser().resolve()
        self.timeout_seconds = float(timeout_seconds)
        self.process: Optional[subprocess.Popen[str]] = None
        self.messages: "queue.Queue[Mapping[str, Any]]" = queue.Queue()
        self.notifications: deque[Mapping[str, Any]] = deque()
        self.stderr: deque[str] = deque(maxlen=40)
        self.next_id = 0
        self.write_lock = threading.Lock()

    def __enter__(self) -> "CodexAppServer":
        if not self.codex_executable.is_file():
            raise CodexProfileError("Codex executable was not found")
        try:
            self.process = subprocess.Popen(
                (str(self.codex_executable), "app-server", "--listen", "stdio://"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=dict(_safe_environment(self.codex_home, self.codex_executable)),
                cwd=str(self.codex_home),
                shell=False,
            )
        except OSError as exc:
            raise CodexProfileError("Codex App Server could not start") from exc
        threading.Thread(target=self._read_stdout, daemon=True, name="codex-app-server-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="codex-app-server-stderr").start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "forge-skill-evolution-studio",
                    "title": "FORGE Skill Evolution Studio",
                    "version": "0.2.1",
                },
                "capabilities": {"experimentalApi": False},
            },
        )
        self.notify("initialized", {})
        return self

    def __exit__(self, *_exc: Any) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for stream in (process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            try:
                value = json.loads(line)
                if isinstance(value, Mapping):
                    self.messages.put(value)
            except json.JSONDecodeError:
                continue

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr.append(line.rstrip()[:1000])

    def _send(self, value: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise CodexProfileError("Codex App Server is not running")
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with self.write_lock:
            try:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
            except OSError as exc:
                raise CodexProfileError("Codex App Server connection closed") from exc

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            message = self._next(deadline)
            if message.get("id") == request_id:
                if message.get("error") is not None:
                    error = message.get("error")
                    detail = error.get("message") if isinstance(error, Mapping) else None
                    raise CodexProfileError(str(detail or "Codex App Server request failed"))
                result = message.get("result", {})
                if not isinstance(result, Mapping):
                    raise CodexProfileError("Codex App Server returned an invalid response")
                return result
            self.notifications.append(message)

    def _next(self, deadline: float) -> Mapping[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexProfileError("Codex App Server timed out")
            try:
                return self.messages.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                process = self.process
                if process is not None and process.poll() is not None:
                    raise CodexProfileError("Codex App Server exited before completing the request")

    def list_models(self) -> Sequence[Mapping[str, Any]]:
        result = self.request("model/list", {"limit": 100, "includeHidden": False})
        models = result.get("data", ())
        if not isinstance(models, Sequence) or isinstance(models, (str, bytes)):
            raise CodexProfileError("Codex returned an invalid model catalog")
        return tuple(item for item in models if isinstance(item, Mapping))

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        effort: Optional[str] = None,
    ) -> Mapping[str, Any]:
        developer_parts = [
            "You are the local analysis brain inside FORGE Skill Evolution Studio. "
            "Follow the supplied analysis instructions exactly. Do not call tools, inspect files, "
            "or modify the environment. Return only the requested analysis result."
        ]
        conversation = []
        for message in messages:
            role = str(message.get("role") or "user").lower()
            content = message.get("content", "")
            if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
                content = "\n".join(str(part.get("text", "")) if isinstance(part, Mapping) else str(part) for part in content)
            text = str(content)
            if role in ("system", "developer"):
                developer_parts.append(text)
            else:
                conversation.append("<%s>\n%s\n</%s>" % (role, text, role))
        prompt = "\n\n".join(conversation) or "Produce the result described by the developer instructions."
        scratch = self.codex_home / "forge-scratch"
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        thread_result = self.request(
            "thread/start",
            {
                "cwd": str(scratch),
                "model": model,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "serviceName": "forge-skill-evolution",
                "baseInstructions": (
                    "You are a text-only inference engine embedded in FORGE. "
                    "Do not use tools or inspect the environment. Follow the developer instructions "
                    "and return only the requested final result."
                ),
                "developerInstructions": "\n\n".join(developer_parts),
            },
        )
        thread = thread_result.get("thread")
        thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexProfileError("Codex did not create an analysis thread")
        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "model": model,
        }
        if effort:
            turn_params["effort"] = effort
        turn_result = self.request("turn/start", turn_params)
        turn = turn_result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, Mapping) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexProfileError("Codex did not start an analysis turn")

        final_answers: list[str] = []
        fallback_answers: list[str] = []
        usage: Mapping[str, Any] = {}
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            message = self.notifications.popleft() if self.notifications else self._next(deadline)
            method = message.get("method")
            params = message.get("params")
            if not isinstance(params, Mapping):
                continue
            if method == "item/completed" and params.get("threadId") == thread_id and params.get("turnId") == turn_id:
                item = params.get("item")
                if isinstance(item, Mapping) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    fallback_answers.append(item["text"])
                    if item.get("phase") == "final_answer":
                        final_answers.append(item["text"])
            elif method == "thread/tokenUsage/updated" and params.get("threadId") == thread_id and params.get("turnId") == turn_id:
                candidate = params.get("tokenUsage")
                if isinstance(candidate, Mapping):
                    usage = candidate
            elif method == "error" and not params.get("willRetry") and params.get("threadId") in (None, thread_id):
                error = params.get("error")
                detail = error.get("message") if isinstance(error, Mapping) else params.get("message")
                if detail:
                    raise CodexProfileError(str(detail)[:1000])
            elif method == "turn/completed" and params.get("threadId") == thread_id:
                completed = params.get("turn")
                if not isinstance(completed, Mapping) or completed.get("id") != turn_id:
                    continue
                if completed.get("status") != "completed":
                    error = completed.get("error")
                    detail = error.get("message") if isinstance(error, Mapping) else None
                    raise CodexProfileError(str(detail or "Codex analysis turn failed"))
                break
        content = (final_answers or fallback_answers)
        if not content or not content[-1].strip():
            raise CodexProfileError("Codex analysis completed without a final answer")
        return {"content": content[-1], "usage": _normalize_usage(usage)}


def _normalize_usage(value: Mapping[str, Any]) -> Mapping[str, int]:
    last = value.get("last") if isinstance(value, Mapping) else None
    if not isinstance(last, Mapping):
        return {}
    mapping = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "totalTokens": "total_tokens",
    }
    result = {}
    for source, target in mapping.items():
        item = last.get(source)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            result[target] = item
    return result


class CodexResponsesClient:
    """Low-token Responses API client configured from an isolated Codex home."""

    def __init__(self, codex_home: Path, timeout_seconds: float = 180) -> None:
        self.codex_home = codex_home.expanduser().resolve()
        self.timeout_seconds = float(timeout_seconds)
        try:
            config = tomllib.loads((self.codex_home / "config.toml").read_text(encoding="utf-8"))
            auth = json.loads((self.codex_home / "auth.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            raise CodexProfileError("The isolated Codex profile cannot be read") from exc
        if not isinstance(config, Mapping) or not isinstance(auth, Mapping):
            raise CodexProfileError("The isolated Codex profile is invalid")
        provider_id = str(config.get("model_provider") or "openai")
        providers = config.get("model_providers")
        provider = providers.get(provider_id) if isinstance(providers, Mapping) else None
        if provider is None and isinstance(providers, Mapping):
            provider = next((item for key, item in providers.items() if str(key).lower() == provider_id.lower()), None)
        if provider is None:
            provider = {}
        if not isinstance(provider, Mapping):
            raise CodexProfileError("The selected Codex model provider is invalid")
        wire_api = str(provider.get("wire_api") or "responses")
        if wire_api != "responses":
            raise CodexProfileError("The low-token Codex path requires a Responses API provider")
        self.base_url = str(provider.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        parsed = urlparse(self.base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if (parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CodexProfileError("Codex provider base_url must be HTTPS or loopback HTTP")
        self.managed_proxy = bool(local_http and provider.get("experimental_bearer_token") == "PROXY_MANAGED")
        env_key = str(provider.get("env_key") or "OPENAI_API_KEY")
        token = "PROXY_MANAGED" if self.managed_proxy else auth.get(env_key) or auth.get("OPENAI_API_KEY")
        if not isinstance(token, str) or not token.strip() or "\x00" in token:
            raise CodexDirectUnavailable("This Codex auth profile requires Codex App Server authentication")
        self._token = token.strip()
        self.disable_storage = bool(config.get("disable_response_storage", True))

    def _json_request(self, method: str, suffix: str, payload: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.base_url + suffix,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "FORGE-Skill-Evolution/0.2.1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except HTTPError as exc:
            try:
                raw_error = exc.read(256 * 1024)
                error_value = json.loads(raw_error.decode("utf-8", errors="replace"))
                error = error_value.get("error") if isinstance(error_value, Mapping) else None
                detail = error.get("message") if isinstance(error, Mapping) else None
            except (OSError, json.JSONDecodeError):
                detail = None
            raise CodexProfileError(str(detail or "Responses API returned HTTP %d" % exc.code)[:1000]) from exc
        except TimeoutError as exc:
            raise CodexProfileError("Responses API model response timed out after %d seconds" % self.timeout_seconds) from exc
        except URLError as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise CodexProfileError("Responses API model response timed out after %d seconds" % self.timeout_seconds) from exc
            raise CodexProfileError("Responses API connection failed") from exc
        except OSError as exc:
            raise CodexProfileError("Responses API connection failed") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise CodexProfileError("Responses API response exceeds the size limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CodexProfileError("Responses API returned invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise CodexProfileError("Responses API returned an invalid response")
        return value

    def _stream_request(self, suffix: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = dict(payload)
        body["stream"] = True
        request = Request(
            self.base_url + suffix,
            data=json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": "FORGE-Skill-Evolution/0.2.1",
            },
        )
        parts: list[str] = []
        completed: Optional[Mapping[str, Any]] = None
        total_bytes = 0
        event_name = ""
        data_lines: list[str] = []

        def consume_event() -> None:
            nonlocal completed, event_name, data_lines
            raw_data = "\n".join(data_lines).strip()
            name = event_name
            event_name = ""
            data_lines = []
            if not raw_data or raw_data == "[DONE]":
                return
            try:
                event = json.loads(raw_data)
            except json.JSONDecodeError as exc:
                raise CodexProfileError("Responses streaming proxy returned invalid SSE JSON") from exc
            if not isinstance(event, Mapping):
                return
            kind = str(event.get("type") or name)
            if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
                parts.append(event["delta"])
            elif kind == "response.output_text.done" and not parts and isinstance(event.get("text"), str):
                parts.append(event["text"])
            elif kind == "response.completed" and isinstance(event.get("response"), Mapping):
                completed = event["response"]
            elif kind in ("response.failed", "response.incomplete", "error"):
                response = event.get("response") if isinstance(event.get("response"), Mapping) else event
                error = response.get("error") if isinstance(response, Mapping) else None
                detail = error.get("message") if isinstance(error, Mapping) else None
                raise CodexProfileError(str(detail or "Responses streaming request failed")[:1000])

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                for raw_line in response:
                    total_bytes += len(raw_line)
                    if total_bytes > 4 * 1024 * 1024:
                        raise CodexProfileError("Responses streaming result exceeds the size limit")
                    try:
                        line = raw_line.decode("utf-8").rstrip("\r\n")
                    except UnicodeError as exc:
                        raise CodexProfileError("Responses streaming proxy returned invalid UTF-8") from exc
                    if not line:
                        consume_event()
                    elif line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                consume_event()
        except HTTPError as exc:
            raise CodexProfileError("Responses streaming proxy returned HTTP %d" % exc.code) from exc
        except TimeoutError as exc:
            raise CodexProfileError("Responses streaming model response timed out after %d seconds" % self.timeout_seconds) from exc
        except URLError as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise CodexProfileError("Responses streaming model response timed out after %d seconds" % self.timeout_seconds) from exc
            raise CodexProfileError("Responses streaming proxy connection failed") from exc
        except OSError as exc:
            raise CodexProfileError("Responses streaming proxy connection failed") from exc

        if not parts and isinstance(completed, Mapping):
            for output in completed.get("output", ()):
                if not isinstance(output, Mapping) or output.get("type") != "message":
                    continue
                for item in output.get("content", ()):
                    if isinstance(item, Mapping) and item.get("type") == "output_text" and isinstance(item.get("text"), str):
                        parts.append(item["text"])
        content = "".join(parts)
        if not content.strip():
            raise CodexProfileError("Responses streaming request completed without output text")
        usage_value = completed.get("usage") if isinstance(completed, Mapping) else None
        usage = {}
        if isinstance(usage_value, Mapping):
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                item = usage_value.get(key)
                if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                    usage[key] = item
        return {"content": content, "usage": usage}

    def list_model_ids(self) -> Sequence[str]:
        value = self._json_request("GET", "/models")
        data = value.get("data")
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise CodexProfileError("Responses provider returned an invalid model list")
        return tuple(str(item["id"]) for item in data if isinstance(item, Mapping) and isinstance(item.get("id"), str))

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        effort: Optional[str] = None,
    ) -> Mapping[str, Any]:
        instructions = [
            "You are the local analysis brain inside FORGE Skill Evolution Studio. "
            "Follow the supplied analysis instructions exactly and return only the requested result."
        ]
        conversation = []
        for message in messages:
            role = str(message.get("role") or "user").lower()
            content = message.get("content", "")
            if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
                content = "\n".join(str(part.get("text", "")) if isinstance(part, Mapping) else str(part) for part in content)
            text = str(content)
            if role in ("system", "developer"):
                instructions.append(text)
            else:
                conversation.append({"role": "assistant" if role == "assistant" else "user", "content": text})
        if not conversation:
            conversation.append({"role": "user", "content": "Produce the result described by the instructions."})
        payload: dict[str, Any] = {
            "model": model,
            "instructions": "\n\n".join(instructions),
            "input": conversation,
            "store": not self.disable_storage,
        }
        if effort:
            payload["reasoning"] = {"effort": effort}
        if self.managed_proxy:
            return self._stream_request("/responses", payload)
        value = self._json_request("POST", "/responses", payload)
        content = value.get("output_text")
        if not isinstance(content, str) or not content:
            parts = []
            for output in value.get("output", ()):
                if not isinstance(output, Mapping) or output.get("type") != "message":
                    continue
                for item in output.get("content", ()):
                    if isinstance(item, Mapping) and item.get("type") == "output_text" and isinstance(item.get("text"), str):
                        parts.append(item["text"])
            content = "".join(parts)
        if not isinstance(content, str) or not content.strip():
            raise CodexProfileError("Responses API completed without output text")
        usage_value = value.get("usage")
        usage = {}
        if isinstance(usage_value, Mapping):
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                item = usage_value.get(key)
                if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                    usage[key] = item
        return {"content": content, "usage": usage}


class CodexProfileManager:
    def __init__(self, root: Path, codex_executable: Optional[str]) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.codex_executable = _absolute_path_preserving_symlinks(codex_executable) if codex_executable else None

    def _directory(self, profile_id: str) -> Path:
        return self.root / _profile_id(profile_id)

    def _metadata_path(self, profile_id: str) -> Path:
        return self._directory(profile_id) / ".forge-profile.json"

    def _metadata(self, profile_id: str) -> dict[str, Any]:
        path = self._metadata_path(profile_id)
        if not path.is_file():
            return {"api_version": PROFILE_API_VERSION, "id": profile_id, "imports": {}, "models": [], "updated_at": None}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"api_version": PROFILE_API_VERSION, "id": profile_id, "imports": {}, "models": [], "updated_at": None}
        return dict(value) if isinstance(value, Mapping) else {"api_version": PROFILE_API_VERSION, "id": profile_id, "imports": {}, "models": [], "updated_at": None}

    def _save_metadata(self, profile_id: str, value: Mapping[str, Any]) -> None:
        _atomic_private_write(
            self._metadata_path(profile_id),
            (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )

    def import_file(self, profile_id: str, kind: str, source_path: str) -> Mapping[str, Any]:
        profile_id = _profile_id(profile_id)
        if kind not in ("config", "auth"):
            raise CodexProfileError("Codex import kind must be config or auth")
        raw = _read_regular_file(Path(str(source_path)))
        safe = _sanitize_config(raw) if kind == "config" else _validate_auth(raw)
        filename = "config.toml" if kind == "config" else "auth.json"
        target = self._directory(profile_id) / filename
        _atomic_private_write(target, safe)
        metadata = self._metadata(profile_id)
        imports = dict(metadata.get("imports") or {})
        imports[kind] = {
            "sha256": hashlib.sha256(safe).hexdigest(),
            "bytes": len(safe),
            "imported_at": _now(),
        }
        metadata.update({"api_version": PROFILE_API_VERSION, "id": profile_id, "imports": imports, "models": [], "updated_at": _now()})
        self._save_metadata(profile_id, metadata)
        return self.profile(profile_id)

    def import_cc_switch(self, profile_id: str, config_path: str, auth_path: str) -> Mapping[str, Any]:
        """Import the active Codex files only when CC Switch local takeover is active."""
        profile_id = _profile_id(profile_id)
        safe_config = _sanitize_config(_read_regular_file(Path(str(config_path))))
        safe_auth = _validate_auth(_read_regular_file(Path(str(auth_path))))
        if not _is_managed_loopback_config(safe_config):
            raise CodexProfileError(
                "CC Switch Codex proxy is not active; enable its Codex proxy or use the two manual import buttons"
            )
        directory = self._directory(profile_id)
        _atomic_private_write(directory / "config.toml", safe_config)
        _atomic_private_write(directory / "auth.json", safe_auth)
        imported_at = _now()
        metadata = self._metadata(profile_id)
        metadata.update({
            "api_version": PROFILE_API_VERSION,
            "id": profile_id,
            "import_source": "cc-switch",
            "imports": {
                "config": {"sha256": hashlib.sha256(safe_config).hexdigest(), "bytes": len(safe_config), "imported_at": imported_at},
                "auth": {"sha256": hashlib.sha256(safe_auth).hexdigest(), "bytes": len(safe_auth), "imported_at": imported_at},
            },
            "models": [],
            "updated_at": imported_at,
        })
        self._save_metadata(profile_id, metadata)
        return self.profile(profile_id)

    def profile(self, profile_id: str) -> Mapping[str, Any]:
        profile_id = _profile_id(profile_id)
        directory = self._directory(profile_id)
        metadata = self._metadata(profile_id)
        config_ready = (directory / "config.toml").is_file()
        auth_ready = (directory / "auth.json").is_file()
        codex_ready = bool(self.codex_executable and self.codex_executable.is_file())
        models = metadata.get("models") if isinstance(metadata.get("models"), list) else []
        inference_mode = "app-server"
        connection_mode = "direct"
        if config_ready:
            try:
                connection_mode = "cc-switch" if _is_managed_loopback_config((directory / "config.toml").read_bytes()) else "direct"
            except OSError:
                pass
        if config_ready and auth_ready:
            try:
                client = CodexResponsesClient(directory, timeout_seconds=1)
                inference_mode = "responses-streaming" if client.managed_proxy else "responses-direct"
            except CodexProfileError:
                pass
        return {
            "api_version": PROFILE_API_VERSION,
            "provider": "codex",
            "id": profile_id,
            "config_ready": config_ready,
            "auth_ready": auth_ready,
            "codex_ready": codex_ready,
            "ready": config_ready and auth_ready and codex_ready,
            "imports": metadata.get("imports") or {},
            "models": models,
            "inference_mode": inference_mode,
            "connection_mode": connection_mode,
            "import_source": metadata.get("import_source") or "manual",
            "updated_at": metadata.get("updated_at"),
        }

    def list_profiles(self) -> Sequence[Mapping[str, Any]]:
        ids = {"default"}
        for item in self.root.iterdir():
            if (
                item.is_dir()
                and _PROFILE_ID.fullmatch(item.name)
                and ((item / "config.toml").is_file() or (item / "auth.json").is_file())
            ):
                ids.add(item.name)
        return tuple(self.profile(profile_id) for profile_id in sorted(ids))

    def health(self) -> Mapping[str, Any]:
        executable = self.codex_executable
        if not executable or not executable.is_file():
            return {"ready": False, "executable": None, "version": None, "error": "Codex CLI was not found"}
        try:
            result = subprocess.run(
                (str(executable), "--version"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
                env=dict(_safe_environment(self.root, executable)),
            )
            version = (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
            return {"ready": result.returncode == 0, "executable": str(executable), "version": version or None, "error": None if result.returncode == 0 else "Codex CLI failed its version check"}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ready": False, "executable": str(executable), "version": None, "error": str(exc)}

    def refresh_models(self, profile_id: str) -> Mapping[str, Any]:
        profile_id = _profile_id(profile_id)
        profile = self.profile(profile_id)
        if not profile["ready"] or self.codex_executable is None:
            raise CodexProfileError("Import both Codex config.toml and auth.json before refreshing models")
        with CodexAppServer(self.codex_executable, self._directory(profile_id), timeout_seconds=45) as server:
            catalog = server.list_models()
        try:
            supported_ids = set(CodexResponsesClient(self._directory(profile_id), timeout_seconds=20).list_model_ids())
            account_catalog = tuple(
                item for item in catalog
                if item.get("model") in supported_ids or item.get("id") in supported_ids
            )
            if account_catalog:
                catalog = account_catalog
        except CodexProfileError:
            pass
        previous = {
            str(item.get("model")): item.get("probe")
            for item in profile.get("models", ())
            if isinstance(item, Mapping) and isinstance(item.get("probe"), Mapping)
        }
        normalized = []
        for item in catalog:
            model = item.get("model")
            if not isinstance(model, str) or not model:
                continue
            efforts = []
            for option in item.get("supportedReasoningEfforts", ()):
                if isinstance(option, Mapping) and isinstance(option.get("reasoningEffort"), str):
                    efforts.append({"id": option["reasoningEffort"], "description": str(option.get("description") or "")})
            normalized_item = {
                "id": str(item.get("id") or model),
                "model": model,
                "display_name": str(item.get("displayName") or model),
                "description": str(item.get("description") or ""),
                "is_default": bool(item.get("isDefault")),
                "default_reasoning_effort": str(item.get("defaultReasoningEffort") or "medium"),
                "reasoning_efforts": efforts,
                "is_gpt": model.lower().startswith("gpt-"),
            }
            if model in previous:
                normalized_item["probe"] = previous[model]
            normalized.append(normalized_item)
        if not normalized:
            raise CodexProfileError("Codex returned no selectable models for this profile")
        metadata = self._metadata(profile_id)
        metadata.update({"api_version": PROFILE_API_VERSION, "id": profile_id, "models": normalized, "updated_at": _now()})
        self._save_metadata(profile_id, metadata)
        return self.profile(profile_id)

    def resolve_model(self, profile_id: str, model_id: str, effort: Optional[str]) -> Mapping[str, str]:
        profile = self.profile(profile_id)
        if not profile["ready"]:
            raise CodexProfileError("The selected Codex profile is incomplete")
        selected = next((item for item in profile["models"] if item.get("id") == model_id or item.get("model") == model_id), None)
        if not isinstance(selected, Mapping):
            raise CodexProfileError("Refresh the Codex model catalog and select an available GPT model")
        if not selected.get("is_gpt"):
            raise CodexProfileError("The local analysis brain must be a GPT model")
        selected_effort = str(effort or selected.get("default_reasoning_effort") or "medium")
        allowed = {str(item.get("id")) for item in selected.get("reasoning_efforts", ()) if isinstance(item, Mapping)}
        if allowed and selected_effort not in allowed:
            raise CodexProfileError("The selected reasoning effort is not supported by this GPT model")
        return {"profile_id": str(profile["id"]), "model_id": str(selected["id"]), "model": str(selected["model"]), "effort": selected_effort}

    def test_model(self, profile_id: str, model_id: str, effort: Optional[str]) -> Mapping[str, Any]:
        selected = self.resolve_model(profile_id, model_id, effort)
        assert self.codex_executable is not None
        started = time.monotonic()
        try:
            try:
                client = CodexResponsesClient(self._directory(profile_id), timeout_seconds=90)
                result = client.complete(
                    ({"role": "user", "content": "Reply with exactly FORGE_READY and nothing else."},),
                    model=selected["model"],
                    effort=selected["effort"],
                )
            except CodexDirectUnavailable:
                with CodexAppServer(self.codex_executable, self._directory(profile_id), timeout_seconds=90) as server:
                    result = server.complete(
                        ({"role": "user", "content": "Reply with exactly FORGE_READY and nothing else."},),
                        model=selected["model"],
                        effort=selected["effort"],
                    )
            # Local streaming proxies may prefix a response with a zero-width
            # format marker. It is transport noise, not part of the fixed probe.
            reply = str(result.get("content") or "").strip().strip(_PROBE_BOUNDARY_FORMAT_CHARS)
            error = None
        except CodexProfileError as exc:
            result = {}
            reply = ""
            error = str(exc)[:1000]
        receipt = {
            "ready": reply == "FORGE_READY",
            "model_id": selected["model_id"],
            "model": selected["model"],
            "reasoning_effort": selected["effort"],
            "reply": reply[:200],
            "usage": result.get("usage") or {},
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": error,
        }
        metadata = self._metadata(profile_id)
        models = []
        for item in metadata.get("models", ()):
            candidate = dict(item)
            if candidate.get("id") == selected["model_id"]:
                candidate["probe"] = {
                    "ready": receipt["ready"],
                    "tested_at": _now(),
                    "reasoning_effort": receipt["reasoning_effort"],
                    "duration_seconds": receipt["duration_seconds"],
                    "usage": receipt["usage"],
                    "error": receipt["error"],
                }
            models.append(candidate)
        metadata.update({"models": models, "updated_at": _now()})
        self._save_metadata(profile_id, metadata)
        return receipt


def codex_env_allowlist() -> Sequence[str]:
    return _SAFE_CHILD_ENV
