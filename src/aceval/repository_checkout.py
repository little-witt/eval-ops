"""Managed local Git checkouts for zero-setup Kernel tasks."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional

from .git_publisher import GitRunner
from .kernel_contracts import RepositoryConfig


class RepositoryCheckoutError(RuntimeError):
    pass


class GitCheckoutManager:
    """Materialize a configured SSH repository without shell interpolation."""

    def __init__(self, runner: Optional[GitRunner] = None) -> None:
        self.runner = runner or GitRunner()

    def _run(self, root: Path, *args: str, timeout: int = 120) -> str:
        result = self.runner.run(("git",) + tuple(args), cwd=root, timeout=timeout)
        if result.returncode != 0:
            raise RepositoryCheckoutError("Git %s failed: %s" % (args[0], result.stderr.strip()[-1000:]))
        return result.stdout.strip()

    def prepare(self, repository: RepositoryConfig, destination: Path) -> RepositoryConfig:
        if repository.local_path is not None:
            return repository
        if destination.is_symlink() or destination.parent.is_symlink():
            raise RepositoryCheckoutError("managed repository destination cannot use symlinks")
        target = destination.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_dir() or not (target / ".git").is_dir():
                raise RepositoryCheckoutError("managed repository destination already exists and is not a Git checkout")
        else:
            self._run(
                target.parent,
                "clone",
                "--branch",
                repository.branch,
                "--single-branch",
                "--",
                repository.ssh_url,
                str(target),
                timeout=300,
            )
        origin = self._run(target, "remote", "get-url", "origin")
        branch = self._run(target, "branch", "--show-current")
        if origin != repository.ssh_url or branch != repository.branch:
            raise RepositoryCheckoutError("managed checkout does not match configured origin and branch")
        if self._run(target, "status", "--porcelain"):
            raise RepositoryCheckoutError("managed checkout must be clean")
        return replace(repository, local_path=str(target))


__all__ = ["GitCheckoutManager", "RepositoryCheckoutError"]
