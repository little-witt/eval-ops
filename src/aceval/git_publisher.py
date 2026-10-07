"""Explicit, post-confirmation publication of a generated Skill candidate."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import subprocess
from typing import Optional, Sequence, Tuple

from .kernel_contracts import RepositoryConfig
from .skill_tree_optimizer import hash_skill_tree, scan_skill_tree


class CandidatePublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitResult:
    argv: Tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class GitRunner:
    def run(self, argv: Sequence[str], *, cwd: Path, timeout: int = 120) -> GitResult:
        values = tuple(str(item) for item in argv)
        try:
            result = subprocess.run(values, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, shell=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CandidatePublishError("Git command could not run") from exc
        return GitResult(values, result.returncode, result.stdout.decode("utf-8", errors="replace"), result.stderr.decode("utf-8", errors="replace"))


class GitSkillPublisher:
    """Atomically publish a manifest-declared Skill candidate file set."""

    def __init__(self, runner: Optional[GitRunner] = None) -> None:
        self.runner = runner or GitRunner()

    def _git(self, root: Path, *args: str, timeout: int = 120) -> str:
        result = self.runner.run(("git",) + tuple(args), cwd=root, timeout=timeout)
        if result.returncode != 0:
            raise CandidatePublishError("Git %s failed: %s" % (args[0], result.stderr.strip()[-1000:]))
        return result.stdout.strip()

    def publish(
        self,
        repository: RepositoryConfig,
        candidate_root: Path,
        *,
        message: str,
    ) -> str:
        if repository.local_path is None:
            raise CandidatePublishError("Skill repository has no local_path")
        skill_root = Path(repository.local_path).expanduser().resolve()
        repo_root = Path(self._git(skill_root, "rev-parse", "--show-toplevel")).resolve()
        branch = self._git(repo_root, "branch", "--show-current")
        if branch != repository.branch:
            raise CandidatePublishError("local checkout branch does not match configured Skill branch")
        origin = self._git(repo_root, "remote", "get-url", "origin")
        if origin != repository.ssh_url:
            raise CandidatePublishError("local checkout origin does not match configured Skill SSH URL")
        if self._git(repo_root, "status", "--porcelain"):
            raise CandidatePublishError("local Skill checkout must be clean before publication")
        candidate_root = Path(candidate_root).expanduser().resolve()
        manifest_path = candidate_root / "candidate.manifest.json"
        manifest = None
        if manifest_path.is_symlink():
            raise CandidatePublishError("candidate manifest cannot be a symlink")
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise CandidatePublishError("candidate manifest is invalid") from exc
            if not isinstance(manifest, dict) or manifest.get("contract") != "aceval.skill-tree-candidate/v1":
                raise CandidatePublishError("candidate manifest contract is unsupported")
            changed_paths = manifest.get("changed_paths")
            if not isinstance(changed_paths, list) or not changed_paths:
                raise CandidatePublishError("candidate manifest changed_paths must be a non-empty array")
            relative_paths = tuple(self._safe_relative_path(item) for item in changed_paths)
            if len(set(relative_paths)) != len(relative_paths):
                raise CandidatePublishError("candidate manifest contains duplicate paths")
            created_values = manifest.get("created_paths", [])
            if not isinstance(created_values, list):
                raise CandidatePublishError("candidate manifest created_paths must be an array")
            created_paths = set(self._safe_relative_path(item) for item in created_values)
            if not created_paths.issubset(set(relative_paths)):
                raise CandidatePublishError("candidate manifest created_paths must be changed paths")
            current_hash = hash_skill_tree(scan_skill_tree(skill_root, require_entrypoint=False))
            parent_hash = manifest.get("parent_subject_hash")
            subject_hash = manifest.get("subject_hash")
            if current_hash == subject_hash:
                commit = self._git(repo_root, "rev-parse", "HEAD")
                if len(commit) != 40:
                    raise CandidatePublishError("Git returned an invalid candidate commit")
                self._git(repo_root, "push", "origin", "HEAD:%s" % repository.branch, timeout=300)
                return commit
            if current_hash != parent_hash:
                raise CandidatePublishError("local Skill tree no longer matches the candidate parent snapshot")
        else:
            # Compatibility with candidates produced before multi-file support.
            legacy = "SKILL.md" if (candidate_root / "SKILL.md").is_file() else "src/SKILL.md"
            relative_paths = (legacy,)
            created_paths = set()

        sources = []
        targets = []
        repo_relatives = []
        originals = {}
        for relative_path in relative_paths:
            source = candidate_root / relative_path
            target = skill_root / relative_path
            if source.is_symlink() or not source.is_file():
                raise CandidatePublishError("candidate must contain a regular resource: %s" % relative_path)
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise CandidatePublishError("checkout target must be a regular resource: %s" % relative_path)
            if not target.exists() and relative_path not in created_paths:
                raise CandidatePublishError("candidate may not create an undeclared resource: %s" % relative_path)
            try:
                repo_relative = target.resolve().relative_to(repo_root)
            except ValueError as exc:
                raise CandidatePublishError("Skill resource is outside configured repository") from exc
            sources.append(source)
            targets.append(target)
            repo_relatives.append(repo_relative.as_posix())
            originals[target] = target.read_bytes() if target.is_file() else None

        equal = [originals[target] is not None and source.read_bytes() == originals[target] for source, target in zip(sources, targets)]
        if all(equal):
            # A previous push may have failed after commit. Retrying the same
            # confirmed candidate must not regenerate or recommit it.
            commit = self._git(repo_root, "rev-parse", "HEAD")
            if len(commit) != 40:
                raise CandidatePublishError("Git returned an invalid candidate commit")
            self._git(repo_root, "push", "origin", "HEAD:%s" % repository.branch, timeout=300)
            return commit
        if any(equal):
            raise CandidatePublishError("local Skill tree only partially matches the candidate")
        try:
            for source, target in zip(sources, targets):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(source), str(target))
            self._git(repo_root, "add", "--", *repo_relatives)
            changed = tuple(filter(None, self._git(repo_root, "diff", "--cached", "--name-only", "--", *repo_relatives).splitlines()))
            if set(changed) != set(repo_relatives):
                raise CandidatePublishError("candidate diff does not match its declared changed paths")
            self._git(repo_root, "commit", "-m", message)
        except Exception:
            # Before a commit exists, restore the exact original working tree.
            try:
                if self._git(repo_root, "diff", "--cached", "--name-only"):
                    self._git(repo_root, "restore", "--staged", "--", *repo_relatives)
                for target, original in originals.items():
                    if original is None:
                        if target.is_file() and not target.is_symlink():
                            target.unlink()
                    else:
                        target.write_bytes(original)
            except Exception:
                pass
            raise
        commit = self._git(repo_root, "rev-parse", "HEAD")
        if len(commit) != 40:
            raise CandidatePublishError("Git returned an invalid candidate commit")
        # A push failure is intentionally recoverable: the local commit stays
        # available and a later publish call can retry without regenerating it.
        self._git(repo_root, "push", "origin", "HEAD:%s" % repository.branch, timeout=300)
        return commit

    def restore_rejected_candidate(
        self,
        repository: RepositoryConfig,
        *,
        challenger_commit: str,
        champion_commit: str,
    ) -> str:
        """Restore champion content with an auditable revert commit.

        Evaluation branches are allowed to move while a challenger is being
        tested, but a rejected challenger must never become the parent of the
        next proposal.  Reverting (instead of resetting) preserves every
        reviewed candidate and the reason it was rejected.
        """

        if repository.local_path is None:
            raise CandidatePublishError("Skill repository has no local_path")
        if not re.fullmatch(r"[0-9a-f]{40}", challenger_commit) or not re.fullmatch(r"[0-9a-f]{40}", champion_commit):
            raise CandidatePublishError("candidate revisions must be full Git commit SHAs")
        skill_root = Path(repository.local_path).expanduser().resolve()
        repo_root = Path(self._git(skill_root, "rev-parse", "--show-toplevel")).resolve()
        if self._git(repo_root, "branch", "--show-current") != repository.branch:
            raise CandidatePublishError("local checkout branch does not match configured Skill branch")
        if self._git(repo_root, "remote", "get-url", "origin") != repository.ssh_url:
            raise CandidatePublishError("local checkout origin does not match configured Skill SSH URL")
        if self._git(repo_root, "status", "--porcelain"):
            raise CandidatePublishError("local Skill checkout must be clean before candidate restoration")
        head = self._git(repo_root, "rev-parse", "HEAD")
        if head != challenger_commit:
            raise CandidatePublishError("rejected challenger is no longer branch HEAD")
        # This command intentionally has no stdout; _git rejects a non-zero
        # status and therefore enforces the ancestry relation.
        self._git(repo_root, "merge-base", "--is-ancestor", champion_commit, challenger_commit)
        self._git(repo_root, "revert", "--no-edit", challenger_commit)
        restored = self._git(repo_root, "rev-parse", "HEAD")
        if len(restored) != 40:
            raise CandidatePublishError("Git returned an invalid restoration commit")
        self._git(repo_root, "push", "origin", "HEAD:%s" % repository.branch, timeout=300)
        return restored

    @staticmethod
    def _safe_relative_path(value: object) -> str:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise CandidatePublishError("candidate manifest contains an unsafe path")
        path = PurePosixPath(value)
        if path.is_absolute() or path.as_posix() != value or any(part in ("", ".", "..") for part in path.parts):
            raise CandidatePublishError("candidate manifest contains an unsafe path")
        return value


__all__ = ["CandidatePublishError", "GitRunner", "GitSkillPublisher"]
