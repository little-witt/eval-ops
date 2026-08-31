"""Safe, token-efficient multi-file Skill candidate generation.

The optimizer edits user-approved existing text resources and may create only
the exact new paths approved by an extend blueprint. Existing content uses an
exact ``old_text`` -> ``new_text`` operation instead of a full repository
rewrite, which keeps prompts and responses small while making every edit
deterministic and reviewable.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .agent_runtime import ModelClient
from .contracts import as_primitive
from .optimizer import CandidateRejected, MaterializedCandidateSnapshot, _skill_file
from .evaluation_skills import node_skill_context


SKILL_TREE_IMPROVER_CONTRACT = "aceval.optimizer/skill-tree-improver-v1"

DEFAULT_EDITABLE_PATTERNS = (
    "SKILL.md",
    "src/SKILL.md",
    "scripts/**",
    "references/**",
    "workflow/**",
    "knowledge/**",
    "specs/**",
    "config/**",
    "assets/**",
)
DEFAULT_PROTECTED_PATTERNS = (
    ".git/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/__pycache__/**",
    "**/*.pyc",
    "skill.manifest",
    "skill.sig",
    "candidate.json",
    "candidate.patch",
    "candidate.manifest.json",
)


@dataclass(frozen=True)
class SkillTreePatchPolicy:
    editable_patterns: Tuple[str, ...] = DEFAULT_EDITABLE_PATTERNS
    protected_patterns: Tuple[str, ...] = DEFAULT_PROTECTED_PATTERNS
    max_changed_files: int = 8
    max_added_lines: int = 300
    max_changed_bytes: int = 512 * 1024
    max_context_chars: int = 120_000
    max_tree_bytes: int = 32 * 1024 * 1024
    forbid_case_literals: bool = True
    validation_commands: Tuple[Tuple[str, ...], ...] = ()
    validation_timeout_seconds: int = 180


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise CandidateRejected("candidate path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(part in ("", ".", "..") for part in path.parts):
        raise CandidateRejected("candidate path is unsafe: %s" % value)
    return value


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _excluded_from_snapshot(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return (
        path == ".git" or path.startswith(".git/")
        or "__pycache__" in parts or name.endswith(".pyc")
        or name == ".env" or name.startswith(".env.")
        or name in ("candidate.json", "candidate.patch", "candidate.manifest.json")
    )


def is_editable_skill_path(path: str, policy: Optional[SkillTreePatchPolicy] = None) -> bool:
    active = policy or SkillTreePatchPolicy()
    try:
        normalized = _relative_path(path)
    except CandidateRejected:
        return False
    return _matches(normalized, active.editable_patterns) and not _matches(normalized, active.protected_patterns)


def scan_skill_tree(
    root: Path,
    policy: Optional[SkillTreePatchPolicy] = None,
    *,
    require_entrypoint: bool = True,
) -> Mapping[str, bytes]:
    """Freeze the repository tree without following links or reading ``.git``."""

    active = policy or SkillTreePatchPolicy()
    source = Path(root).expanduser().resolve()
    if not source.is_dir() or source.is_symlink():
        raise CandidateRejected("Skill tree must be a regular directory")
    files: Dict[str, bytes] = {}
    total = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if _excluded_from_snapshot(relative):
            continue
        if path.is_symlink():
            raise CandidateRejected("Skill tree symlink is not allowed: %s" % relative)
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise CandidateRejected("cannot read Skill resource: %s" % relative) from exc
        total += len(raw)
        if total > active.max_tree_bytes:
            raise CandidateRejected("Skill tree exceeds max_tree_bytes")
        files[relative] = raw
    if require_entrypoint:
        entry = _skill_file(source).relative_to(source).as_posix()
        if entry not in files:
            raise CandidateRejected("Skill entrypoint is not part of the frozen tree")
    return files


def editable_skill_inventory(root: Path, policy: Optional[SkillTreePatchPolicy] = None) -> Tuple[str, ...]:
    active = policy or SkillTreePatchPolicy()
    return tuple(path for path in scan_skill_tree(root, active) if is_editable_skill_path(path, active))


def hash_skill_tree(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(b"aceval-skill-tree-v1\0")
    for path in sorted(files):
        name = path.encode("utf-8")
        content = files[path]
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _decode_editable(path: str, raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateRejected("editable Skill resource must be UTF-8 text: %s" % path) from exc


def _compact_model_value(value: Any, *, max_string: int = 1_600, max_items: int = 20, depth: int = 0) -> Any:
    """Bound analysis evidence before it is sent to the edit model.

    A decision contains full per-Case attempts and trace references for audit,
    but sending that whole envelope again at the edit stage can exceed a local
    model's context and surface as an opaque optimizer failure.  Preserve the
    shape and high-signal prefixes while keeping the immutable files on disk
    as the source of truth.
    """

    if depth >= 5:
        return _bounded_text(value, max_string)
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string].rstrip() + "…"
    if isinstance(value, Mapping):
        return {
            str(key): _compact_model_value(item, max_string=max_string, max_items=max_items, depth=depth + 1)
            for key, item in list(value.items())[:max_items]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _compact_model_value(item, max_string=max_string, max_items=max_items, depth=depth + 1)
            for item in list(value)[:max_items]
        ]
    return value


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _normalize_entrypoint_alias(path: Any, scope: Sequence[str]) -> str:
    value = str(path or "").strip()
    allowed = set(str(item) for item in scope)
    if value == "SKILL.md" and value not in allowed and "src/SKILL.md" in allowed:
        return "src/SKILL.md"
    return value


def _candidate_json_response(content: str) -> Mapping[str, Any]:
    """Extract one complete candidate JSON object from a model reply."""

    if not isinstance(content, str):
        raise ValueError("candidate response is not text")
    text = content.lstrip("\ufeff").strip()
    candidates = [text]
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE):
        candidates.append(match.group(1).strip())
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            candidates.append(text[index:index + end])
            break
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            return value
    raise ValueError("candidate response contains no JSON object")


def _validate_changed_file(path: str, content: str) -> None:
    if not content.strip():
        raise CandidateRejected("changed Skill resource must not be empty: %s" % path)
    if path.endswith(".json"):
        try:
            json.loads(content, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise CandidateRejected("changed JSON resource is invalid: %s" % path) from exc
    if path.endswith(".py"):
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            raise CandidateRejected("changed Python resource is invalid: %s" % path) from exc


def _copy_tree(source: Path, destination: Path) -> None:
    source = Path(source).expanduser().resolve()
    def ignore(directory: str, names: Sequence[str]) -> set:
        relative = Path(directory).resolve().relative_to(source).as_posix()
        ignored = set()
        for name in names:
            path = name if relative == "." else "%s/%s" % (relative, name)
            if _excluded_from_snapshot(path):
                ignored.add(name)
        return ignored

    shutil.copytree(source, destination, symlinks=False, ignore=ignore)


def _run_validations(root: Path, commands: Sequence[Sequence[str]], timeout: int) -> Tuple[Mapping[str, Any], ...]:
    receipts = []
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for command in commands:
        argv = tuple(str(item) for item in command)
        if not argv or any(not item or "\x00" in item for item in argv):
            raise CandidateRejected("validation command contains an invalid argv item")
        try:
            result = subprocess.run(
                argv,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                shell=False,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CandidateRejected("candidate validation command could not run") from exc
        receipt = {
            "argv": list(argv),
            "returncode": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace")[-4000:],
            "stderr": result.stderr.decode("utf-8", errors="replace")[-4000:],
        }
        receipts.append(receipt)
        if result.returncode != 0:
            raise CandidateRejected("candidate validation command failed: %s" % argv[0])
    return tuple(receipts)


class SkillTreeOptimizer:
    id = "skill_tree_v1"
    proposal_contract = SKILL_TREE_IMPROVER_CONTRACT
    supported_modes = frozenset(("repair", "tune", "extend"))

    def __init__(self, model_client: ModelClient) -> None:
        self._model = model_client

    def propose(
        self,
        subject: Path,
        failures: Sequence[Any],
        output_root: Path,
        *,
        target_scope: Sequence[str],
        allow_create_paths: Sequence[str] = (),
        policy: Optional[SkillTreePatchPolicy] = None,
        forbidden_literals: Iterable[str] = (),
    ) -> MaterializedCandidateSnapshot:
        if not failures:
            raise CandidateRejected("optimizer requires at least one dev evidence item")
        active = policy or SkillTreePatchPolicy()
        source = Path(subject).expanduser().resolve()
        frozen = scan_skill_tree(source, active)
        inventory = tuple(path for path in frozen if is_editable_skill_path(path, active))
        create_paths = tuple(dict.fromkeys(_relative_path(str(path)) for path in allow_create_paths))
        invalid_create = [path for path in create_paths if path in frozen or not is_editable_skill_path(path, active)]
        if invalid_create:
            raise CandidateRejected("create path must be a new editable Skill resource: %s" % ", ".join(invalid_create))
        scope = tuple(
            dict.fromkeys(
                _normalize_entrypoint_alias(_relative_path(str(path)), inventory)
                for path in target_scope
            )
        )
        if not scope:
            scope = (_skill_file(source).relative_to(source).as_posix(),)
        unsupported = [path for path in scope if path not in inventory and path not in create_paths]
        if unsupported:
            raise CandidateRejected("approved target scope is not an editable existing resource: %s" % ", ".join(unsupported))
        current_files = {path: _decode_editable(path, frozen[path]) for path in scope if path in frozen}
        context_chars = sum(len(path) + len(content) for path, content in current_files.items())
        if context_chars > active.max_context_chars:
            raise CandidateRejected("approved target scope exceeds optimizer context limit; select more specific files")
        request = {
            "current_files": current_files,
            "editable_resource_inventory": list(inventory),
            "approved_target_scope": list(scope),
            "approved_create_paths": [path for path in scope if path in create_paths],
            "evidence": [
                _compact_model_value(as_primitive(item), max_string=1_600, max_items=20)
                for item in failures
            ],
            "output_contract": {
                "changes": [
                    {"path": "approved/existing/file", "operation": "replace_text", "old_text": "exact unique text", "new_text": "replacement", "reason": "evidence-based reason"},
                    {"path": "approved/new/file", "operation": "create_file", "content": "complete UTF-8 content", "reason": "capability requires a bundled resource"}
                ],
                "rationale": "short cross-case explanation",
            },
        }
        if len(scope) == 1 and scope[0] in ("SKILL.md", "src/SKILL.md") and scope[0] in current_files:
            request["current_skill"] = current_files[scope[0]]
        system_prompt = (
            node_skill_context("skill-optimizer") + " "
            "Repair, tune, or extend the Agent Skill using only supplied evidence and approved files. "
            "Return strict JSON with a NON-EMPTY `changes` array (or `skill_markdown` only for a single SKILL.md target). "
            "Each changes item must be an executable replace_text/create_file operation; do not return proposed_changes, "
            "an empty array, a prose-only plan, or a rationale without edits. Use the smallest exact replace_text edits; "
            "old_text must occur exactly once. Do not emit whole unchanged files, case IDs, fixture literals, grader internals, "
            "or hidden data. Only create files explicitly listed in approved_create_paths. Never delete, rename, or edit "
            "paths outside approved_target_scope."
        )
        reply = self._model.complete(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            (),
        )
        if reply.tool_calls:
            raise CandidateRejected("optimizer may not call tools", reply.usage)
        try:
            payload = _candidate_json_response(reply.content)
        except ValueError:
            payload = None
        # Backward compatibility keeps existing single-file model bridges usable
        # while the desktop migrates to the multi-file contract.
        if isinstance(payload, Mapping) and isinstance(payload.get("skill_markdown"), str) and len(scope) == 1 and scope[0] in current_files:
            changes = [{"path": scope[0], "operation": "replace_text", "old_text": current_files[scope[0]], "new_text": payload["skill_markdown"], "reason": payload.get("rationale", "")}]
        elif isinstance(payload, Mapping):
            changes = payload.get("changes")
        else:
            changes = None
        # The analysis stage uses a different ``proposed_changes`` shape. A
        # local model can accidentally echo that plan here instead of
        # producing executable edits. Give it one narrowly-scoped repair turn
        # before failing, rather than surfacing the opaque empty-array error.
        if not isinstance(changes, list) or not changes:
            repair_request = dict(request)
            repair_request["previous_response"] = payload if isinstance(payload, Mapping) else str(reply.content)[:4000]
            repair_request["repair_instruction"] = (
                "上一次输出不是可解析的可执行编辑。请不要使用 Markdown 围栏或前后说明；仅返回一个 JSON 对象：changes 必须是非空数组；"
                "每项使用 approved_target_scope 内的 replace_text（提供唯一 old_text/new_text）或允许的 create_file。"
            )
            repair_reply = self._model.complete(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(repair_request, ensure_ascii=False)},
                ],
                (),
            )
            if repair_reply.tool_calls:
                raise CandidateRejected("optimizer may not call tools", repair_reply.usage)
            try:
                repair_payload = _candidate_json_response(repair_reply.content)
            except ValueError as exc:
                raise CandidateRejected("optimizer returned invalid repair JSON", repair_reply.usage) from exc
            payload = repair_payload
            if isinstance(payload, Mapping) and isinstance(payload.get("skill_markdown"), str) and len(scope) == 1 and scope[0] in current_files:
                changes = [{"path": scope[0], "operation": "replace_text", "old_text": current_files[scope[0]], "new_text": payload["skill_markdown"], "reason": payload.get("rationale", "")}]
            elif isinstance(payload, Mapping):
                changes = payload.get("changes")
            else:
                changes = None
            reply = repair_reply
        if isinstance(changes, list):
            # Accept the conventional root alias when the approved checkout
            # contains only src/SKILL.md, then keep materialization strict for
            # every other path.
            changes = [
                dict(item, path=_normalize_entrypoint_alias(item.get("path"), scope))
                if isinstance(item, Mapping) and item.get("path")
                else item
                for item in changes
            ]
        if not isinstance(changes, list) or not changes or any(not isinstance(item, Mapping) for item in changes):
            raise CandidateRejected("optimizer changes must be a non-empty array", reply.usage)
        try:
            return materialize_skill_tree_candidate(
                source,
                frozen,
                changes,
                str(payload.get("rationale", "")),
                Path(output_root),
                scope,
                active,
                create_paths=create_paths,
                forbidden_literals=forbidden_literals,
                usage=reply.usage,
            )
        except CandidateRejected as exc:
            # Models often select a short sentence that appears more than
            # once (or return an ellipsis-truncated excerpt).  The exact
            # replacement contract is correct, but surfacing this low-level
            # validation error makes an otherwise recoverable proposal look
            # like a failed optimization.  Give the model one repair turn
            # with the concrete occurrence error and the frozen file text;
            # never weaken the uniqueness check in materialization.
            if "old_text must occur exactly once" not in str(exc):
                raise CandidateRejected(str(exc), reply.usage) from exc
            repair_request = dict(request)
            repair_request["previous_response"] = payload if isinstance(payload, Mapping) else str(reply.content)[:4000]
            repair_request["repair_instruction"] = (
                "上一次编辑的 old_text 在目标文件中不是唯一匹配，已被拒绝。请重新返回严格 JSON。"
                "old_text 必须从 current_files 原文逐字复制、不能包含省略号，并选择带标题/代码上下文的唯一连续片段；"
                "不要输出整文件。若无法安全选择唯一片段，只对 SKILL.md 返回完整 skill_markdown。"
            )
            repair_reply = self._model.complete(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(repair_request, ensure_ascii=False)},
                ],
                (),
            )
            if repair_reply.tool_calls:
                raise CandidateRejected("optimizer may not call tools", repair_reply.usage)
            try:
                repaired_payload = _candidate_json_response(repair_reply.content)
            except ValueError as repair_exc:
                raise CandidateRejected("optimizer returned invalid repair JSON", repair_reply.usage) from repair_exc
            if isinstance(repaired_payload, Mapping) and isinstance(repaired_payload.get("skill_markdown"), str) and len(scope) == 1 and scope[0] in current_files:
                repaired_changes = [{"path": scope[0], "operation": "replace_text", "old_text": current_files[scope[0]], "new_text": repaired_payload["skill_markdown"], "reason": repaired_payload.get("rationale", "")}]
            elif isinstance(repaired_payload, Mapping):
                repaired_changes = repaired_payload.get("changes")
            else:
                repaired_changes = None
            if isinstance(repaired_changes, list):
                repaired_changes = [
                    dict(item, path=_normalize_entrypoint_alias(item.get("path"), scope))
                    if isinstance(item, Mapping) and item.get("path") else item
                    for item in repaired_changes
                ]
            if not isinstance(repaired_changes, list) or not repaired_changes or any(not isinstance(item, Mapping) for item in repaired_changes):
                raise CandidateRejected("optimizer changes must be a non-empty array", repair_reply.usage)
            try:
                return materialize_skill_tree_candidate(
                    source,
                    frozen,
                    repaired_changes,
                    str(repaired_payload.get("rationale", "")) if isinstance(repaired_payload, Mapping) else "",
                    Path(output_root),
                    scope,
                    active,
                    create_paths=create_paths,
                    forbidden_literals=forbidden_literals,
                    usage=repair_reply.usage,
                )
            except CandidateRejected as repair_exc:
                raise CandidateRejected(str(repair_exc), repair_reply.usage) from repair_exc


def materialize_skill_tree_candidate(
    source: Path,
    parent_files: Mapping[str, bytes],
    changes: Sequence[Mapping[str, Any]],
    rationale: str,
    output_root: Path,
    approved_scope: Sequence[str],
    policy: SkillTreePatchPolicy,
    create_paths: Sequence[str] = (),
    forbidden_literals: Iterable[str] = (),
    usage: Optional[Mapping[str, Any]] = None,
) -> MaterializedCandidateSnapshot:
    if len(changes) > policy.max_changed_files * 8:
        raise CandidateRejected("candidate contains too many edit operations")
    allowed_create = set(create_paths)
    texts = {path: _decode_editable(path, parent_files[path]) for path in approved_scope if path in parent_files}
    changed_reasons: Dict[str, list] = {}
    for change in changes:
        path = _relative_path(str(change.get("path", "")))
        if path not in approved_scope:
            raise CandidateRejected("candidate edits an unapproved path: %s" % path)
        operation = change.get("operation")
        if operation == "create_file":
            content = change.get("content")
            if path not in allowed_create or path in parent_files:
                raise CandidateRejected("candidate may only create an approved new path: %s" % path)
            if path in texts or not isinstance(content, str) or not content.strip():
                raise CandidateRejected("create_file requires one non-empty UTF-8 content value")
            texts[path] = content
            changed_reasons.setdefault(path, []).append(str(change.get("reason", "")))
            continue
        if operation != "replace_text":
            raise CandidateRejected("candidate operation must be replace_text or create_file")
        if path not in parent_files:
            raise CandidateRejected("replace_text requires an existing resource: %s" % path)
        old = change.get("old_text")
        new = change.get("new_text")
        if not isinstance(old, str) or not old or not isinstance(new, str) or old == new:
            raise CandidateRejected("replace_text requires distinct non-empty old_text and string new_text")
        if texts[path].count(old) != 1:
            raise CandidateRejected("old_text must occur exactly once in %s" % path)
        texts[path] = texts[path].replace(old, new, 1)
        changed_reasons.setdefault(path, []).append(str(change.get("reason", "")))
    created_paths = tuple(sorted(path for path in texts if path not in parent_files))
    changed_paths = tuple(sorted(path for path in texts if path not in parent_files or texts[path].encode("utf-8") != parent_files[path]))
    if not changed_paths:
        raise CandidateRejected("candidate does not change any approved Skill resource")
    if len(changed_paths) > policy.max_changed_files:
        raise CandidateRejected("candidate exceeds max_changed_files")

    diffs = []
    added_lines = 0
    changed_bytes = 0
    literals = tuple(item for item in forbidden_literals if isinstance(item, str) and len(item) >= 8)
    candidate_files = dict(parent_files)
    for path in changed_paths:
        content = texts[path]
        _validate_changed_file(path, content)
        original = _decode_editable(path, parent_files[path]) if path in parent_files else ""
        if policy.forbid_case_literals and any(literal in content and literal not in original for literal in literals):
            raise CandidateRejected("candidate contains test-only literal")
        encoded = content.encode("utf-8")
        changed_bytes += len(encoded)
        candidate_files[path] = encoded
        lines = list(difflib.unified_diff(
            original.splitlines(),
            content.splitlines(),
            fromfile="a/%s" % path,
            tofile="b/%s" % path,
            lineterm="",
        ))
        added_lines += sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        diffs.extend(lines)
    if added_lines > policy.max_added_lines:
        raise CandidateRejected("candidate exceeds max_added_lines")
    if changed_bytes > policy.max_changed_bytes:
        raise CandidateRejected("candidate exceeds max_changed_bytes")

    parent_hash = hash_skill_tree(parent_files)
    subject_hash = hash_skill_tree(candidate_files)
    snapshot_id = subject_hash[:16]
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / snapshot_id
    patch = "\n".join(diffs) + "\n"
    manifest = {
        "contract": "aceval.skill-tree-candidate/v1",
        "snapshot_id": snapshot_id,
        "subject_hash": subject_hash,
        "parent_subject_hash": parent_hash,
        "changed_paths": list(changed_paths),
        "created_paths": list(created_paths),
        "approved_target_scope": list(approved_scope),
        "change_reasons": changed_reasons,
        "rationale": rationale,
    }
    if destination.exists():
        marker = destination / "candidate.manifest.json"
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CandidateRejected("candidate hash collision") from exc
        if existing.get("subject_hash") != subject_hash or tuple(existing.get("changed_paths", ())) != changed_paths:
            raise CandidateRejected("candidate hash collision")
        validation = tuple(existing.get("validation", ()))
    else:
        with tempfile.TemporaryDirectory(prefix="skill-tree-candidate-", dir=str(output_root)) as temp_value:
            staged = Path(temp_value) / "tree"
            _copy_tree(source, staged)
            for path in changed_paths:
                target = staged / path
                if path in created_paths:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() or target.is_symlink():
                        raise CandidateRejected("candidate create target already exists: %s" % path)
                elif target.is_symlink() or not target.is_file():
                    raise CandidateRejected("candidate target changed during materialization: %s" % path)
                target.write_bytes(candidate_files[path])
            validation = _run_validations(staged, policy.validation_commands, policy.validation_timeout_seconds)
            manifest["validation"] = list(validation)
            (staged / "candidate.patch").write_text(patch, encoding="utf-8")
            (staged / "candidate.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            shutil.copytree(staged, destination)
        for path in destination.rglob("*"):
            if path.is_file() and not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o222)

    try:
        entry = _skill_file(source).relative_to(source).as_posix()
        parent_file_hash = hashlib.sha256(parent_files[entry]).hexdigest()
    except CandidateRejected:
        parent_file_hash = hashlib.sha256(b"").hexdigest()
    return MaterializedCandidateSnapshot(
        snapshot_id=snapshot_id,
        path=destination,
        subject_hash=subject_hash,
        parent_hash=parent_hash,
        parent_file_hash=parent_file_hash,
        patch=patch,
        rationale=rationale,
        usage=dict(usage or {}),
        changed_paths=changed_paths,
        created_paths=created_paths,
        validation=validation,
    )


__all__ = [
    "DEFAULT_EDITABLE_PATTERNS",
    "DEFAULT_PROTECTED_PATTERNS",
    "SKILL_TREE_IMPROVER_CONTRACT",
    "SkillTreeOptimizer",
    "SkillTreePatchPolicy",
    "editable_skill_inventory",
    "hash_skill_tree",
    "is_editable_skill_path",
    "materialize_skill_tree_candidate",
    "scan_skill_tree",
]
