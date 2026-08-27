"""Isolated Claude Code profiles imported from an active CC Switch proxy."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


PROFILE_API_VERSION = "aceval.claude-profile/v1"
_MAX_IMPORT_BYTES = 1024 * 1024
_FORMAT_CHARS = "\u200b\u200c\u200d\u2060\ufeff"


class ClaudeProfileError(RuntimeError):
    """A safe, user-displayable Claude profile error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_regular_file(path: Path) -> bytes:
    if path.is_symlink():
        raise ClaudeProfileError("Claude settings source must not be a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise ClaudeProfileError("Claude settings cannot be opened") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size <= 0 or details.st_size > _MAX_IMPORT_BYTES:
            raise ClaudeProfileError("Claude settings must be a non-empty regular file below 1MB")
        return os.read(descriptor, _MAX_IMPORT_BYTES + 1)
    finally:
        os.close(descriptor)


def _atomic_private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".forge-import-", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _validated_settings(raw: bytes) -> Mapping[str, str]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ClaudeProfileError("Claude settings.json is not valid JSON") from exc
    environment = value.get("env") if isinstance(value, Mapping) else None
    if not isinstance(environment, Mapping):
        raise ClaudeProfileError("Claude settings.json does not contain an env object")
    base_url = environment.get("ANTHROPIC_BASE_URL")
    token = environment.get("ANTHROPIC_AUTH_TOKEN") or environment.get("ANTHROPIC_API_KEY")
    model = value.get("model") if isinstance(value, Mapping) else None
    parsed = urlparse(str(base_url or ""))
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ClaudeProfileError("CC Switch Claude proxy is not active on a loopback HTTP address")
    if not isinstance(token, str) or not token.strip():
        raise ClaudeProfileError("CC Switch Claude configuration has no authentication token")
    if not isinstance(model, str) or not model.strip():
        raise ClaudeProfileError("CC Switch Claude configuration has no active model")
    return {"base_url": str(base_url).rstrip("/"), "token": token.strip(), "model": model.strip()}


class ClaudeMessagesClient:
    def __init__(self, profile_path: Path, timeout_seconds: float = 180) -> None:
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ClaudeProfileError("FORGE Claude profile cannot be read") from exc
        self.base_url = str(profile.get("base_url") or "").rstrip("/")
        self.token = str(profile.get("token") or "")
        self.timeout_seconds = float(timeout_seconds)
        if not self.base_url or not self.token:
            raise ClaudeProfileError("FORGE Claude profile is incomplete")

    def _headers(self) -> Mapping[str, str]:
        return {
            "authorization": "Bearer " + self.token,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "user-agent": "FORGE-Skill-Evolution/0.2.1",
        }

    @staticmethod
    def _models_from_error(detail: str) -> Sequence[str]:
        match = re.search(r"Available models:\s*(.+)$", detail)
        return tuple(item.strip() for item in match.group(1).split(",") if item.strip()) if match else ()

    def list_model_ids(self) -> Sequence[str]:
        request = Request(self.base_url + "/v1/models", headers=dict(self._headers()), method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                value = json.loads(response.read(1024 * 1024).decode("utf-8"))
        except (HTTPError, URLError, OSError, TimeoutError, UnicodeError, json.JSONDecodeError):
            value = {}
        raw_models = value.get("data") or value.get("models") or () if isinstance(value, Mapping) else ()
        ids = []
        for item in raw_models:
            model_id = item.get("id") or item.get("model") if isinstance(item, Mapping) else item
            if isinstance(model_id, str) and model_id.strip():
                ids.append(model_id.strip())
        if ids:
            return tuple(dict.fromkeys(ids))
        # Some CC Switch versions return an empty catalog but include the
        # provider-scoped allowed ids in a bounded invalid-model response.
        body = {"model": "__forge_model_catalog__", "max_tokens": 1, "messages": [{"role": "user", "content": "catalog"}]}
        discovery = Request(self.base_url + "/v1/messages", data=json.dumps(body).encode("utf-8"), method="POST", headers=dict(self._headers()))
        try:
            with urlopen(discovery, timeout=self.timeout_seconds):
                return ()
        except HTTPError as exc:
            try:
                error_value = json.loads(exc.read(256 * 1024).decode("utf-8", errors="replace"))
                error = error_value.get("error") if isinstance(error_value, Mapping) else None
                detail = str(error.get("message") or "") if isinstance(error, Mapping) else ""
            except json.JSONDecodeError:
                detail = ""
            return self._models_from_error(detail)
        except (URLError, OSError, TimeoutError):
            return ()

    def complete(self, messages: Sequence[Mapping[str, Any]], *, model: str, effort: Optional[str] = None) -> Mapping[str, Any]:
        system_parts = []
        conversation = []
        for message in messages:
            role = str(message.get("role") or "user").lower()
            content = str(message.get("content") or "")
            if role in ("system", "developer"):
                system_parts.append(content)
            else:
                conversation.append({"role": "assistant" if role == "assistant" else "user", "content": content})
        if not conversation:
            conversation.append({"role": "user", "content": "Produce the requested result."})
        body = {"model": model, "max_tokens": 24000, "messages": conversation}
        if effort and effort != "default":
            body["output_config"] = {"effort": effort}
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        attempted_models = []
        while True:
            attempted_models.append(str(body["model"]))
            request = Request(
                self.base_url + "/v1/messages",
                data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                method="POST",
                headers=dict(self._headers()),
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    raw = response.read(4 * 1024 * 1024 + 1)
                break
            except HTTPError as exc:
                error_raw = exc.read(256 * 1024)
                try:
                    error_value = json.loads(error_raw.decode("utf-8", errors="replace"))
                    error = error_value.get("error") if isinstance(error_value, Mapping) else None
                    detail = str(error.get("message") or "") if isinstance(error, Mapping) else ""
                except json.JSONDecodeError:
                    detail = ""
                # CC Switch can store Claude Code convenience aliases such as
                # ``sonnet[1m]`` while its active provider requires a concrete
                # model id.  Its bounded 403 response lists the allowed ids;
                # select the newest matching family and retry once.
                family = re.split(r"[\[\s]", str(model).lower(), maxsplit=1)[0]
                available = list(self._models_from_error(detail))
                family_matches = [item for item in available if family and family in item.lower()]
                if family_matches:
                    resolved = family_matches[-1]
                else:
                    # CC Switch can retain an alias from another provider
                    # (for example ``kimi-k2.5``) while the active Claude
                    # group advertises only Anthropic model ids. Pick the
                    # strongest compatible advertised family explicitly and
                    # expose the resolved id to the caller; never silently
                    # keep reporting the rejected alias as the model used.
                    preferred = [
                        item for item in available if "sonnet" in item.lower()
                    ] or [item for item in available if "opus" in item.lower()] or available
                    resolved = preferred[-1] if preferred else None
                if exc.code == 403 and resolved and resolved not in attempted_models and len(attempted_models) == 1:
                    body["model"] = resolved
                    continue
                raise ClaudeProfileError(str(detail or "Claude proxy returned HTTP %d" % exc.code)[:1000]) from exc
            except (URLError, OSError, TimeoutError) as exc:
                raise ClaudeProfileError("Claude proxy connection failed") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise ClaudeProfileError("Claude proxy response exceeds the size limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ClaudeProfileError("Claude proxy returned invalid JSON") from exc
        parts = [item.get("text", "") for item in value.get("content", ()) if isinstance(item, Mapping) and item.get("type") == "text"]
        content = "".join(str(item) for item in parts)
        if not content.strip():
            raise ClaudeProfileError("Claude proxy completed without output text")
        source_usage = value.get("usage") if isinstance(value.get("usage"), Mapping) else {}
        input_tokens = int(source_usage.get("input_tokens") or 0)
        output_tokens = int(source_usage.get("output_tokens") or 0)
        return {"content": content, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}, "resolved_model": str(body["model"])}


class ClaudeProfileManager:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve() / "claude-default"

    @property
    def profile_path(self) -> Path:
        return self.root / "profile.json"

    @property
    def metadata_path(self) -> Path:
        return self.root / ".forge-profile.json"

    def import_cc_switch(self, settings_path: str) -> Mapping[str, Any]:
        settings = _validated_settings(_read_regular_file(Path(settings_path)))
        safe = json.dumps(settings, ensure_ascii=False, sort_keys=True).encode("utf-8")
        _atomic_private_write(self.profile_path, safe)
        metadata = {
            "api_version": PROFILE_API_VERSION,
            "provider": "claude",
            "id": "claude-default",
            "import_source": "cc-switch-claude",
            "model": settings["model"],
            "models": [],
            "sha256": hashlib.sha256(safe).hexdigest(),
            "updated_at": _now(),
        }
        _atomic_private_write(self.metadata_path, json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        return self.profile()

    def profile(self) -> Mapping[str, Any]:
        try:
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            model = str(metadata["model"])
        except (OSError, KeyError, UnicodeError, json.JSONDecodeError):
            return {"api_version": PROFILE_API_VERSION, "provider": "claude", "id": "claude-default", "ready": False, "models": [], "import_source": "manual"}
        stored_models = metadata.get("models") if isinstance(metadata.get("models"), list) else []
        if stored_models:
            models = stored_models
        else:
            models = [{"id": model, "model": model, "display_name": model, "description": "Claude Code model alias from the active CC Switch profile", "is_default": True, "is_gpt": False, "selectable": True, "default_reasoning_effort": "default", "reasoning_efforts": []}]
        return {"api_version": PROFILE_API_VERSION, "provider": "claude", "id": "claude-default", "ready": self.profile_path.is_file(), "config_ready": True, "auth_ready": True, "codex_ready": False, "models": models, "connection_mode": "cc-switch", "inference_mode": "claude-messages", "import_source": metadata.get("import_source"), "updated_at": metadata.get("updated_at")}

    def refresh_models(self) -> Mapping[str, Any]:
        profile = self.profile()
        if not profile["ready"]:
            raise ClaudeProfileError("Import the active CC Switch Claude configuration first")
        ids = ClaudeMessagesClient(self.profile_path, 45).list_model_ids()
        if not ids:
            raise ClaudeProfileError("CC Switch Claude proxy returned no selectable models")
        configured = str(json.loads(self.metadata_path.read_text(encoding="utf-8")).get("model") or "")
        family = configured.lower().split("[")[0]
        family_matches = [model_id for model_id in ids if family and family in model_id.lower()]
        default_model = family_matches[-1] if family_matches else ids[0]
        normalized = []
        for model_id in ids:
            supports_effort = "haiku" not in model_id.lower()
            effort_ids = ("low", "medium", "high", "xhigh", "max") if supports_effort else ()
            normalized.append({
                "id": model_id, "model": model_id, "display_name": model_id,
                "description": "Claude model exposed by the active CC Switch provider",
                "is_default": model_id == default_model,
                "is_gpt": False, "selectable": True,
                "default_reasoning_effort": "high" if supports_effort else "default",
                "reasoning_efforts": [{"id": item, "description": "Claude adaptive thinking effort"} for item in effort_ids],
            })
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        previous = {item.get("model"): item.get("probe") for item in metadata.get("models", ()) if isinstance(item, Mapping)}
        for item in normalized:
            if previous.get(item["model"]):
                item["probe"] = previous[item["model"]]
        metadata.update({"models": normalized, "updated_at": _now()})
        _atomic_private_write(self.metadata_path, json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        return self.profile()

    def resolve_model(self, model_id: str, effort: Optional[str] = None) -> Mapping[str, str]:
        profile = self.profile()
        selected = next((item for item in profile["models"] if item["id"] == model_id or item["model"] == model_id), None)
        if not profile["ready"] or not selected:
            raise ClaudeProfileError("Import the active CC Switch Claude configuration first")
        selected_effort = str(effort or selected.get("default_reasoning_effort") or "default")
        allowed = {str(item.get("id")) for item in selected.get("reasoning_efforts", ()) if isinstance(item, Mapping)}
        if allowed and selected_effort not in allowed:
            raise ClaudeProfileError("The selected Claude effort is not supported by this model")
        if not allowed:
            selected_effort = "default"
        return {"profile_id": "claude-default", "model_id": str(selected["id"]), "model": str(selected["model"]), "effort": selected_effort}

    def test_model(self, model_id: str, effort: Optional[str] = None) -> Mapping[str, Any]:
        selected = self.resolve_model(model_id, effort)
        started = time.monotonic()
        try:
            result = ClaudeMessagesClient(self.profile_path, 90).complete(({"role": "user", "content": "Reply with exactly FORGE_READY and nothing else."},), model=selected["model"], effort=selected["effort"])
            reply = str(result.get("content") or "").strip().strip(_FORMAT_CHARS)
            error: Optional[str] = None
        except ClaudeProfileError as exc:
            result, reply, error = {}, "", str(exc)[:1000]
        receipt = {"ready": reply == "FORGE_READY", "model_id": selected["model_id"], "model": selected["model"], "reasoning_effort": selected["effort"], "reply": reply[:200], "usage": result.get("usage") or {}, "duration_seconds": round(time.monotonic() - started, 3), "error": error}
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        models = []
        for item in metadata.get("models", ()):
            candidate = dict(item)
            if candidate.get("id") == selected["model_id"]:
                candidate["probe"] = {key: receipt[key] for key in ("ready", "reasoning_effort", "duration_seconds", "usage", "error")}
            models.append(candidate)
        metadata["models"] = models
        metadata["updated_at"] = _now()
        _atomic_private_write(self.metadata_path, json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        return receipt
