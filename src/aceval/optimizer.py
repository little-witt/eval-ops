"""Constrained Skill Markdown candidate generation."""

from __future__ import annotations

import difflib
import hashlib
import inspect
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .agent_runtime import ModelClient, ReferenceRuntimeError
from .contracts import (
    CANDIDATE_PATCH_OPTIMIZER_CONTRACT,
    CandidatePatch,
    PatchConstraints,
    SubjectSnapshot,
    as_primitive,
)
from .subjects import SkillMarkdownSubjectAdapter, hash_skill_subject


SKILL_MARKDOWN_GENERATOR_CONTRACT = "aceval.optimizer/skill-markdown-generator-v1"
SKILL_MARKDOWN_IMPROVER_CONTRACT = "aceval.optimizer/skill-markdown-improver-v2"


class CandidateRejected(ValueError):
    """Raised when a proposed candidate violates its patch policy."""

    def __init__(self, message: str, usage: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.usage = dict(usage or {})


@dataclass(frozen=True)
class FailureEvidence:
    scenario_id: str
    grader_id: str
    summary: str
    evidence: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class TuneEvidence:
    mode: str
    goal: str
    objective: Mapping[str, Any]
    baseline_value: float
    scenario_values: Mapping[str, float]


@dataclass(frozen=True)
class MaterializedCandidateSnapshot:
    snapshot_id: str
    path: Path
    subject_hash: str
    parent_hash: str
    parent_file_hash: str
    patch: str
    rationale: str
    usage: Mapping[str, Any] = None
    changed_paths: Tuple[str, ...] = ()
    created_paths: Tuple[str, ...] = ()
    validation: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class SkillPatchPolicy:
    max_added_lines: int = 30
    max_bytes: int = 64 * 1024
    forbid_case_literals: bool = True


class SkillMarkdownOptimizer:
    id = "skill_markdown_v1"
    proposal_contract = SKILL_MARKDOWN_IMPROVER_CONTRACT
    supported_modes = frozenset(("repair", "tune"))

    def __init__(self, model_client: ModelClient) -> None:
        self._model = model_client

    def propose(
        self,
        subject: Path,
        failures: Sequence[Any],
        output_root: Path,
        policy: Optional[SkillPatchPolicy] = None,
        forbidden_literals: Iterable[str] = (),
    ) -> MaterializedCandidateSnapshot:
        if not failures:
            raise CandidateRejected("optimizer requires at least one dev evidence item")
        active_policy = policy or SkillPatchPolicy()
        skill_file = _skill_file(subject)
        original = skill_file.read_text(encoding="utf-8")
        serialized_evidence = [as_primitive(item) for item in failures]
        request = {
            "current_skill": original,
            "evidence": serialized_evidence,
            "output_contract": {
                "skill_markdown": "complete replacement SKILL.md",
                "rationale": "short evidence-based explanation",
            },
        }
        reply = self._model.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Repair or tune the Agent Skill using only the supplied dev evidence. "
                        "Return one JSON object with skill_markdown and rationale. "
                        "Do not mention case IDs, fixture literals, grader internals, or hidden data."
                    ),
                },
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            (),
        )
        if reply.tool_calls:
            raise CandidateRejected("optimizer may not call tools", reply.usage)
        try:
            payload = json.loads(reply.content)
            candidate = payload["skill_markdown"]
            rationale = payload.get("rationale", "")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise CandidateRejected(
                "optimizer returned invalid candidate JSON", reply.usage
            ) from exc
        try:
            return materialize_candidate(
                subject=subject,
                candidate_markdown=candidate,
                rationale=str(rationale),
                output_root=output_root,
                policy=active_policy,
                forbidden_literals=forbidden_literals,
                usage=reply.usage,
            )
        except CandidateRejected as exc:
            raise CandidateRejected(str(exc), reply.usage) from exc


class FrozenCandidateOptimizer:
    """Loads a preregistered candidate for deterministic conformance runs."""

    id = "skill_markdown_v1"
    proposal_contract = SKILL_MARKDOWN_GENERATOR_CONTRACT
    supported_modes = frozenset(("repair", "tune"))

    def __init__(self, candidate: Path) -> None:
        self._candidate = candidate

    def propose(
        self,
        subject: Path,
        failures: Sequence[Any],
        output_root: Path,
        policy: Optional[SkillPatchPolicy] = None,
        forbidden_literals: Iterable[str] = (),
    ) -> MaterializedCandidateSnapshot:
        if not failures:
            raise CandidateRejected("optimizer requires at least one dev evidence item")
        candidate_text = _skill_file(self._candidate).read_text(encoding="utf-8")
        return materialize_candidate(
            subject=subject,
            candidate_markdown=candidate_text,
            rationale="preregistered deterministic candidate",
            output_root=output_root,
            policy=policy or SkillPatchPolicy(),
            forbidden_literals=forbidden_literals,
        )


class SkillOptimizerBridge:
    """Adapt the D20 Skill generator to the Kernel's generic Optimizer contract.

    The semantic generator proposes complete Markdown. This bridge validates and
    materializes it, then exposes an immutable ``CandidatePatch`` whose base hash
    is the full frozen Subject hash rather than an ad-hoc file hash.
    """

    id = "skill_markdown_v1"
    proposal_contract = CANDIDATE_PATCH_OPTIMIZER_CONTRACT

    def __init__(
        self,
        generator: Any,
        output_root: Path,
        policy: SkillPatchPolicy,
        forbidden_literals: Iterable[str] = (),
    ) -> None:
        self._generator = generator
        self._output_root = Path(output_root)
        self._policy = policy
        self._forbidden_literals = tuple(forbidden_literals)

    async def propose(
        self,
        base: SubjectSnapshot,
        diagnoses: Sequence[Any],
        constraints: PatchConstraints,
    ) -> Sequence[CandidatePatch]:
        allowed = tuple(constraints.allowed_paths)
        if allowed and "SKILL.md" not in allowed:
            raise CandidateRejected("optimizer constraints do not allow SKILL.md")
        if not isinstance(base.content, str):
            raise CandidateRejected("optimizer requires frozen Skill Markdown content")
        self._output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="frozen-parent-", dir=str(self._output_root)
        ) as parent_value:
            parent_path = Path(parent_value)
            SkillMarkdownSubjectAdapter().materialize(base, parent_path)
            proposed = self._generator.propose(
                subject=parent_path,
                failures=diagnoses,
                output_root=self._output_root,
                policy=self._policy,
                forbidden_literals=self._forbidden_literals,
            )
            if inspect.isawaitable(proposed):
                proposed = await proposed
            actual_parent_file_hash = _hash_text(base.content)
        if proposed.parent_hash != base.content_hash:
            raise CandidateRejected(
                "candidate parent Subject hash mismatch", proposed.usage
            )
        if proposed.parent_file_hash != actual_parent_file_hash:
            raise CandidateRejected(
                "candidate parent file hash mismatch", proposed.usage
            )
        patch_hash = hashlib.sha256(proposed.patch.encode("utf-8")).hexdigest()
        return (
            CandidatePatch(
                base_hash=base.content_hash,
                unified_diff=proposed.patch,
                allowed_paths=allowed or ("SKILL.md",),
                rationale=proposed.rationale,
                patch_hash=patch_hash,
                metadata={
                    "snapshot_id": proposed.snapshot_id,
                    "path": str(proposed.path),
                    "subject_hash": proposed.subject_hash,
                    "parent_subject_hash": proposed.parent_hash,
                    "parent_file_hash": proposed.parent_file_hash,
                    "usage": dict(proposed.usage or {}),
                },
            ),
        )


def materialize_candidate(
    subject: Path,
    candidate_markdown: str,
    rationale: str,
    output_root: Path,
    policy: SkillPatchPolicy,
    forbidden_literals: Iterable[str] = (),
    usage: Optional[Mapping[str, Any]] = None,
) -> MaterializedCandidateSnapshot:
    if not isinstance(candidate_markdown, str) or not candidate_markdown.strip():
        raise CandidateRejected("candidate SKILL.md is empty")
    encoded = candidate_markdown.encode("utf-8")
    if len(encoded) > policy.max_bytes:
        raise CandidateRejected("candidate SKILL.md exceeds byte limit")

    parent_snapshot = SkillMarkdownSubjectAdapter().snapshot(str(subject))
    original = str(parent_snapshot.content)
    if candidate_markdown == original:
        raise CandidateRejected("candidate does not change SKILL.md")
    if policy.forbid_case_literals:
        leaked = [literal for literal in forbidden_literals if len(literal) >= 8 and literal in candidate_markdown]
        if leaked:
            raise CandidateRejected("candidate contains test-only literal")

    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(),
            candidate_markdown.splitlines(),
            fromfile="a/SKILL.md",
            tofile="b/SKILL.md",
            lineterm="",
        )
    )
    added_lines = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    if added_lines > policy.max_added_lines:
        raise CandidateRejected("candidate exceeds max_added_lines")

    parent_subject_hash = parent_snapshot.content_hash
    parent_file_hash = _hash_text(original)
    manifest_bytes = parent_snapshot.files.get("subject.json")
    subject_hash = hash_skill_subject(encoded, manifest_bytes)
    snapshot_id = subject_hash[:16]
    snapshot_path = output_root / snapshot_id
    if snapshot_path.exists():
        existing = SkillMarkdownSubjectAdapter().snapshot(str(snapshot_path))
        if existing.content_hash != subject_hash or dict(existing.files) != {
            key: value
            for key, value in (
                ("SKILL.md", encoded),
                ("subject.json", manifest_bytes),
            )
            if value is not None
        }:
            raise CandidateRejected("candidate hash collision")
    else:
        snapshot_path.mkdir(parents=True, exist_ok=False)
        skill_target = snapshot_path / "SKILL.md"
        skill_target.write_bytes(encoded)
        skill_target.chmod(0o444)
        if manifest_bytes is not None:
            manifest_target = snapshot_path / "subject.json"
            manifest_target.write_bytes(manifest_bytes)
            manifest_target.chmod(0o444)
        (snapshot_path / "candidate.patch").write_text("\n".join(diff_lines) + "\n", encoding="utf-8")
        (snapshot_path / "candidate.json").write_text(
            json.dumps(
                {
                    "snapshot_id": snapshot_id,
                    "subject_hash": subject_hash,
                    "parent_subject_hash": parent_subject_hash,
                    "parent_file_hash": parent_file_hash,
                    "rationale": rationale,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return MaterializedCandidateSnapshot(
        snapshot_id=snapshot_id,
        path=snapshot_path,
        subject_hash=subject_hash,
        parent_hash=parent_subject_hash,
        parent_file_hash=parent_file_hash,
        patch="\n".join(diff_lines) + "\n",
        rationale=rationale,
        usage=dict(usage or {}),
    )


def _skill_file(subject: Path) -> Path:
    path = subject.resolve()
    if path.is_dir():
        path = path / "SKILL.md"
    if not path.is_file():
        raise CandidateRejected("SKILL.md not found: %s" % path)
    return path


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# Compatibility alias for callers of the initial prototype. New Kernel code
# uses CandidatePatch plus MaterializedCandidateSnapshot to avoid confusing it
# with contracts.CandidateSnapshot (the lineage record).
CandidateSnapshot = MaterializedCandidateSnapshot


__all__ = [
    "CandidateRejected",
    "CandidateSnapshot",
    "FailureEvidence",
    "TuneEvidence",
    "FrozenCandidateOptimizer",
    "MaterializedCandidateSnapshot",
    "SkillMarkdownOptimizer",
    "SkillOptimizerBridge",
    "SkillPatchPolicy",
    "SKILL_MARKDOWN_GENERATOR_CONTRACT",
    "SKILL_MARKDOWN_IMPROVER_CONTRACT",
    "materialize_candidate",
]
