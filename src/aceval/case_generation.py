"""Compile a :class:`TestPlan` into editable Pack Builder case drafts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence, Tuple

from .test_planning import CaseDraft, TestPlan


CASE_GENERATION_API_VERSION = "aceval.case-generation/v1"
_PLANNING_ONLY_CASE_FIELDS = frozenset(
    (
        "expected_observables",
        "family_id",
        "oracle_level",
        "required_runtime_capabilities",
        "requirement_ids",
    )
)


class CaseGenerationError(ValueError):
    pass


def _seed_map(seed_cases: Sequence[Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    values = {}
    for item in seed_cases:
        if not isinstance(item, Mapping):
            raise CaseGenerationError("seed cases must be objects")
        case_id = item.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise CaseGenerationError("seed cases require non-empty ids")
        if case_id in values:
            raise CaseGenerationError("duplicate seed case id: %s" % case_id)
        values[case_id] = dict(item)
    return values


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _metadata(case: CaseDraft) -> Mapping[str, Any]:
    return {
        "requirement_ids": list(case.requirement_ids),
        "family_id": case.family,
        "origin": case.origin,
        "oracle_trust": case.oracle_level,
        "oracle_ready": case.oracle_ready,
        "executable": case.executable,
        "expected_observables": list(case.expected_observables),
        "required_runtime_capabilities": list(
            case.required_runtime_capabilities
        ),
        "needs_user_input": case.needs_user_input,
        "selection_reason": case.selection_reason,
    }


@dataclass(frozen=True)
class CaseGenerationResult:
    cases_document: Mapping[str, Any]
    generation_provenance: Mapping[str, Any]
    active_case_ids: Tuple[str, ...]
    generated_case_ids: Tuple[str, ...]
    pending_oracle_case_ids: Tuple[str, ...]
    api_version: str = CASE_GENERATION_API_VERSION

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "api_version": self.api_version,
            "cases_document": dict(self.cases_document),
            "generation_provenance": dict(self.generation_provenance),
            "active_case_ids": list(self.active_case_ids),
            "generated_case_ids": list(self.generated_case_ids),
            "pending_oracle_case_ids": list(self.pending_oracle_case_ids),
        }


def compile_case_drafts(
    plan: TestPlan,
    seed_cases: Sequence[Mapping[str, Any]] = (),
    *,
    name: str = "planned-skill-eval",
    version: str = "0.1.0",
) -> CaseGenerationResult:
    """Create an editable cases document without inventing semantic Oracles.

    Original seed fixtures and exact expected values are preserved.  Generated
    cases contain prompts and provenance only; unless the planner could prove a
    trusted Oracle they intentionally remain calibration blockers.
    """

    if not isinstance(plan, TestPlan):
        raise CaseGenerationError("plan must be a TestPlan")
    seeds = _seed_map(seed_cases)
    output = []
    generated = []
    pending = []
    for case in plan.cases:
        if case.origin == "seed":
            if case.id not in seeds:
                raise CaseGenerationError(
                    "plan seed case is missing from source cases: %s" % case.id
                )
            value = dict(seeds[case.id])
            if "oracle_ref" in value:
                raise CaseGenerationError(
                    "case %s uses oracle_ref, which the Pack Builder cannot "
                    "snapshot; inline a supported expected value instead" % case.id
                )
            for key in _PLANNING_ONLY_CASE_FIELDS:
                value.pop(key, None)
        else:
            value = {
                "id": case.id,
                "prompt": case.prompt,
                "split": case.split,
                "tags": ["aceval-generated", case.origin],
            }
            generated.append(case.id)
        existing_metadata = value.get("metadata", {})
        if existing_metadata is None:
            existing_metadata = {}
        if not isinstance(existing_metadata, Mapping):
            raise CaseGenerationError(
                "case %s metadata must be an object" % case.id
            )
        metadata = dict(existing_metadata)
        if "aceval_test" in metadata:
            raise CaseGenerationError(
                "case %s already defines reserved metadata.aceval_test" % case.id
            )
        metadata["aceval_test"] = _metadata(case)
        value["metadata"] = metadata
        value.setdefault("split", case.split)
        if not case.oracle_ready or not any(
            key in value
            for key in ("expected_output", "expected_records", "forbidden_records")
        ):
            pending.append(case.id)
        output.append(value)

    document = {
        "name": name,
        "version": version,
        "description": (
            "Generated from a source-grounded Test Plan. Generated semantic "
            "Oracles remain drafts until calibration."
        ),
        "cases": output,
    }
    provenance = {
        "api_version": CASE_GENERATION_API_VERSION,
        "subject_hash": plan.subject_hash,
        "test_plan_hash": _canonical_hash(plan.to_dict()),
        "seed_cases_hash": _canonical_hash(list(seed_cases)),
        "case_count": len(output),
        "generated_case_count": len(generated),
        "pending_oracle_case_ids": list(pending),
        "generator": "aceval.case-generation/deterministic-v1",
        "mutation_score": None,
    }
    return CaseGenerationResult(
        cases_document=document,
        generation_provenance=provenance,
        active_case_ids=tuple(item["id"] for item in output),
        generated_case_ids=tuple(generated),
        pending_oracle_case_ids=tuple(pending),
    )


__all__ = [
    "CASE_GENERATION_API_VERSION",
    "CaseGenerationError",
    "CaseGenerationResult",
    "compile_case_drafts",
]
