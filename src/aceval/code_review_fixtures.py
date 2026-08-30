"""Model-authored, immutable code-review fixtures.

The evaluator must review a real pull-request-shaped change, not the default
branch of a source repository.  This module turns a frozen Case and the
evaluated Skill into a small committed branch in an isolated Git worktree.
The source checkout is never switched or dirtied; only the generated branch
is pushed to the configured origin so a remote session can fetch it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence, Union

from .agent_runtime import ModelClient


FIXTURE_GENERATION_API_VERSION = "aceval.code-review-fixture-generation/v1"


class CodeReviewFixtureGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedFixture:
    case_id: str
    branch: str
    base_ref: str
    base_commit: str
    head_commit: str
    changed_paths: tuple[str, ...]
    diff_sha256: str
    rationale: str
    evaluation_flow_id: Optional[str] = None

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": FIXTURE_GENERATION_API_VERSION,
            "case_id": self.case_id,
            "fixture_branch": self.branch,
            "base_ref": self.base_ref,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "changed_paths": list(self.changed_paths),
            "diff_sha256": self.diff_sha256,
            "rationale": self.rationale,
            "evaluation_flow_id": self.evaluation_flow_id,
        }


def _git(root: Path, *args: str, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            ("git",) + tuple(args), cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            check=False, shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodeReviewFixtureGenerationError("Git command could not run") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-1200:]
        raise CodeReviewFixtureGenerationError("Git %s failed: %s" % (args[0], detail))
    return result.stdout.decode("utf-8", errors="replace").strip()


def _remote_branch_exists(root: Path, branch: str) -> bool:
    """Return whether origin already advertises branch, without mutating refs."""
    try:
        output = _git(root, "ls-remote", "--heads", "origin", "refs/heads/%s" % branch, timeout=30)
    except CodeReviewFixtureGenerationError:
        # A missing origin (or an unavailable remote) is reported by the
        # eventual push with its original, actionable error.
        return False
    return bool(output)


def _safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CodeReviewFixtureGenerationError("generated fixture contains an unsafe path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in ("", ".", "..") for part in path.parts):
        raise CodeReviewFixtureGenerationError("generated fixture contains an unsafe path")
    if ".git" in path.parts:
        raise CodeReviewFixtureGenerationError("generated fixture may not modify .git")
    return value


def _case_slug(value: str) -> str:
    """Make an ASCII Git-ref/worktree component while retaining Case identity."""

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")[:64]
    return slug or ("case-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12])


def _json_response(content: str) -> Mapping[str, Any]:
    raw = str(content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodeReviewFixtureGenerationError("fixture model returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise CodeReviewFixtureGenerationError("fixture model response must be an object")
    return value


def _inventory(root: Path, *, limit: int = 96_000) -> str:
    files = [item for item in _git(root, "ls-files", "-z").split("\x00") if item]
    chunks = []
    used = 0
    for relative in files:
        path = root / _safe_path(relative)
        if not path.is_file() or path.is_symlink():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        excerpt = content[:8000]
        chunk = "\n--- %s ---\n%s" % (relative, excerpt)
        if used + len(chunk) > limit:
            break
        chunks.append(chunk)
        used += len(chunk)
    return "".join(chunks) or "（仓库没有可读取的文本文件）"


def _changes(value: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], str]:
    rows = value.get("changes", value.get("files"))
    if not isinstance(rows, list) or not rows or any(not isinstance(item, Mapping) for item in rows):
        raise CodeReviewFixtureGenerationError("fixture model changes must be a non-empty array")
    if len(rows) > 12:
        raise CodeReviewFixtureGenerationError("fixture model returned too many changed files")
    normalized = []
    for item in rows:
        path = _safe_path(item.get("path"))
        operation = str(item.get("operation") or "").strip().lower()
        if not operation:
            operation = "create_file" if "content" in item else "replace_text"
        if operation not in ("replace_text", "append_text", "create_file", "delete_text"):
            raise CodeReviewFixtureGenerationError("unsupported fixture operation: %s" % operation)
        row = {"path": path, "operation": operation}
        if operation == "replace_text":
            if not isinstance(item.get("old_text"), str) or not item["old_text"] or not isinstance(item.get("new_text"), str):
                raise CodeReviewFixtureGenerationError("replace_text requires old_text and new_text")
            row.update({"old_text": item["old_text"], "new_text": item["new_text"]})
        elif operation == "append_text":
            if not isinstance(item.get("new_text", item.get("content")), str):
                raise CodeReviewFixtureGenerationError("append_text requires text")
            row["new_text"] = item.get("new_text", item.get("content"))
        elif operation == "create_file":
            # Models commonly call this field `new_text` when reusing the
            # replace_text shape. Accept the harmless aliases while keeping a
            # strict string requirement and never treating missing content as
            # an empty file.
            content = item.get("content", item.get("new_text", item.get("text")))
            if not isinstance(content, str):
                raise CodeReviewFixtureGenerationError("create_file requires content")
            row["content"] = content
        else:
            if not isinstance(item.get("old_text"), str) or not item["old_text"]:
                raise CodeReviewFixtureGenerationError("delete_text requires old_text")
            row["old_text"] = item["old_text"]
        normalized.append(row)
    return normalized, str(value.get("rationale") or "").strip()[:2000]


def _apply_changes(root: Path, changes: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    changed = []
    for item in changes:
        relative = str(item["path"])
        target = root / relative
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise CodeReviewFixtureGenerationError("fixture target is not a regular file: %s" % relative)
        operation = item["operation"]
        if operation == "create_file":
            if target.exists():
                raise CodeReviewFixtureGenerationError("fixture create target already exists: %s" % relative)
            content = str(item["content"])
        else:
            if not target.is_file():
                raise CodeReviewFixtureGenerationError("fixture edit target does not exist: %s" % relative)
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise CodeReviewFixtureGenerationError("fixture target is not UTF-8 text: %s" % relative) from exc
            old = str(item.get("old_text") or "")
            if operation == "replace_text":
                if content.count(old) != 1:
                    raise CodeReviewFixtureGenerationError("fixture replacement must match exactly once: %s" % relative)
                content = content.replace(old, str(item["new_text"]), 1)
            elif operation == "delete_text":
                if content.count(old) != 1:
                    raise CodeReviewFixtureGenerationError("fixture deletion must match exactly once: %s" % relative)
                content = content.replace(old, "", 1)
            else:
                content += str(item["new_text"])
        if len(content.encode("utf-8")) > 256 * 1024:
            raise CodeReviewFixtureGenerationError("fixture file exceeds 256 KiB: %s" % relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        changed.append(relative)
    return tuple(dict.fromkeys(changed))


def _prompt(
    *,
    skill_text: str,
    case: Mapping[str, Any],
    inventory: str,
    base_commit: str,
    evaluation_flow_id: Optional[str] = None,
) -> str:
    case_meta = case.get("metadata", {}) if isinstance(case.get("metadata"), Mapping) else {}
    test = case_meta.get("aceval_test", {}) if isinstance(case_meta.get("aceval_test"), Mapping) else {}
    context = {
        "case_id": str(case.get("id") or ""),
        "case_title": str(case.get("title") or test.get("title") or ""),
        "case_prompt": str(case.get("prompt") or ""),
        "expected_observables": list(test.get("expected_observables", ())),
        "source_refs": list(test.get("source_refs", ())),
        "base_commit": base_commit,
        # The flow id is deliberately included in the generator context. It
        # prevents two otherwise identical evaluation flows from receiving
        # byte-for-byte identical fixture instructions/content and gives the
        # generated branch a stable audit anchor.
        "evaluation_flow_id": str(evaluation_flow_id or ""),
    }
    return (
        "你是 FORGE 的代码评审 Fixture 生成模型。请根据被评测 Skill 的完整内容和冻结 Case，"
        "在真实仓库 base commit 上生成一个小而真实、可审阅的 PR 改动，使该 Case 的评审目标被覆盖。"
        "改动必须只涉及业务源码或必要文档，不要修改测试、CI、依赖锁文件、凭据或 .git；不要加入评测提示、Case ID 或隐藏答案。"
        "优先使用 replace_text 修改已有文件，只有确有必要时才 create_file/append_text。每个替换必须精确命中一次。"
        "只返回严格 JSON：{\"api_version\":\"aceval.code-review-fixture-generation/v1\",\"changes\":["
        "{\"path\":\"src/file.ts\",\"operation\":\"replace_text\",\"old_text\":\"...\",\"new_text\":\"...\",\"reason\":\"...\"}],"
        "\"rationale\":\"...\"}。不要 Markdown。\n\n冻结 Case：%s\n\n"
        # Keep fixture-generation requests comfortably below provider proxy
        # limits.  The settings probe is tiny, while the previous request
        # combined a 24K Skill excerpt with a 96K repository snapshot and
        # could make CC Switch fail upstream with HTTP 502.  Each Case gets
        # the same bounded, relevant context; the immutable branch remains
        # the source of truth for the actual diff.
        "被评测 Skill：\n%s\n\n仓库文件快照：\n%s" % (
            json.dumps(context, ensure_ascii=False, indent=2), skill_text[:16_000], inventory[:40_000],
        )
    )


def generate_code_review_fixtures(
    model: ModelClient,
    *,
    repository: Union[str, Path],
    skill_text: str,
    cases: Sequence[Mapping[str, Any]],
    output_root: Union[str, Path],
    branch_prefix: str = "aceval/case",
    base_ref: str = "HEAD",
    push: bool = True,
    evaluation_flow_id: Optional[str] = None,
) -> tuple[GeneratedFixture, ...]:
    """Generate and commit one independent PR branch per Case."""

    root = Path(repository).expanduser().resolve()
    if not root.is_dir() or (root / ".git").is_symlink():
        raise CodeReviewFixtureGenerationError("code-review fixture repository is invalid")
    if _git(root, "status", "--porcelain"):
        raise CodeReviewFixtureGenerationError("code-review fixture repository must be clean before generation")
    base_ref = str(base_ref or "HEAD").strip()
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", base_ref) or ".." in base_ref or "@{" in base_ref:
        raise CodeReviewFixtureGenerationError("fixture base ref is unsafe")
    base_commit = _git(root, "rev-parse", "%s^{commit}" % base_ref).lower()
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise CodeReviewFixtureGenerationError("fixture base commit is invalid")
    inventory = _inventory(root, limit=40_000)
    work_root = Path(output_root).expanduser().resolve() / "worktrees"
    work_root.mkdir(parents=True, exist_ok=True)
    generated = []
    for case in cases:
        case_id = str(case.get("id") or "").strip()
        if not case_id or len(case_id) > 128 or any(ord(char) < 32 for char in case_id):
            raise CodeReviewFixtureGenerationError("Case id cannot be used for a fixture branch: %s" % case_id)
        branch = "%s/%s" % (branch_prefix.strip("/"), _case_slug(case_id))
        # A previous run may have successfully pushed this deterministic Case
        # branch while the client failed before persisting its metadata. Never
        # overwrite that remote branch; choose a unique ref for this run.
        if push and _remote_branch_exists(root, branch):
            branch = "%s-%s-%s" % (
                branch,
                datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                uuid.uuid4().hex[:8],
            )
        if len(branch) > 180 or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
            raise CodeReviewFixtureGenerationError("generated fixture branch is unsafe")
        # A pre-existing branch is never overwritten. ``rev-parse`` keeps the
        # failure diagnosable while the explicit else handles an existing ref.
        try:
            _git(root, "rev-parse", "--verify", "refs/heads/%s" % branch, timeout=20)
        except CodeReviewFixtureGenerationError as exc:
            if "Git rev-parse failed" not in str(exc):
                raise
        else:
            # A previous attempt may have created the local branch but failed
            # before publishing its receipt. Never overwrite it; allocate a
            # timestamped branch for this new generation attempt.
            branch = "%s-%s-%s" % (
                branch,
                datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                uuid.uuid4().hex[:8],
            )
            while True:
                try:
                    _git(root, "rev-parse", "--verify", "refs/heads/%s" % branch, timeout=20)
                except CodeReviewFixtureGenerationError as exc:
                    if "Git rev-parse failed" in str(exc):
                        break
                    raise
                branch = "%s-%s" % (branch, uuid.uuid4().hex[:8])
        worktree = work_root / (_case_slug(case_id) + "-" + hashlib.sha256(case_id.encode()).hexdigest()[:8])
        if worktree.exists():
            raise CodeReviewFixtureGenerationError("fixture worktree already exists: %s" % worktree)
        _git(root, "worktree", "add", "--detach", str(worktree), base_commit, timeout=180)
        try:
            _git(worktree, "switch", "-c", branch)
            reply = model.complete(
                [
                    {"role": "system", "content": "Return only a safe, minimal JSON code-review fixture patch."},
                    {"role": "user", "content": _prompt(skill_text=skill_text, case=case, inventory=inventory, base_commit=base_commit, evaluation_flow_id=evaluation_flow_id)},
                ],
                (),
            )
            if getattr(reply, "tool_calls", ()):
                raise CodeReviewFixtureGenerationError("fixture generator may not call tools")
            payload = _json_response(reply.content)
            changes, rationale = _changes(payload)
            changed_paths = _apply_changes(worktree, changes)
            _git(worktree, "add", "--", *changed_paths)
            if not _git(worktree, "diff", "--cached", "--name-only").splitlines():
                raise CodeReviewFixtureGenerationError("fixture generator produced no diff")
            commit_label = "aceval: fixture %s" % case_id
            if evaluation_flow_id:
                commit_label += " (%s)" % str(evaluation_flow_id)[:48]
            _git(worktree, "commit", "--no-gpg-sign", "-m", commit_label)
            head_commit = _git(worktree, "rev-parse", "HEAD").lower()
            diff = subprocess.run(
                ("git", "diff", "--binary", "%s..%s" % (base_commit, head_commit)),
                cwd=str(worktree), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=120, check=False,
            )
            if diff.returncode != 0:
                raise CodeReviewFixtureGenerationError("cannot capture generated fixture diff")
            digest = hashlib.sha256(diff.stdout).hexdigest()
            if push:
                try:
                    _git(worktree, "push", "origin", "HEAD:%s" % branch, timeout=300)
                except CodeReviewFixtureGenerationError as exc:
                    # The remote can change between ls-remote above and this
                    # push. On a fetch-first/non-fast-forward race, publish
                    # this immutable commit under a fresh ref instead of
                    # overwriting the branch that won the race.
                    detail = str(exc).lower()
                    if not any(marker in detail for marker in ("fetch first", "non-fast-forward", "rejected")):
                        raise
                    branch = "%s-%s" % (branch, uuid.uuid4().hex[:10])
                    _git(worktree, "push", "origin", "HEAD:%s" % branch, timeout=300)
            generated.append(GeneratedFixture(case_id, branch, base_ref, base_commit, head_commit, changed_paths, "sha256:" + digest, rationale, evaluation_flow_id))
        finally:
            _git(root, "worktree", "remove", "--force", str(worktree), timeout=120)
    return tuple(generated)


__all__ = [
    "FIXTURE_GENERATION_API_VERSION",
    "CodeReviewFixtureGenerationError",
    "GeneratedFixture",
    "generate_code_review_fixtures",
]
