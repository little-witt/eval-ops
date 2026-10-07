"""CATX RuntimeAdapter with exact Skill/repository binding evidence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import time
from typing import Callable

from .catx import CatxAgentClient
from .catx_bindings import CatxBindingError, CatxExecutionBinding
from .contracts import (
    PreparedScenario,
    RunContext,
    RuntimeCapabilities,
    RuntimeResult,
    SubjectSnapshot,
)


CatxBindingResolver = Callable[
    [PreparedScenario, SubjectSnapshot, RunContext], CatxExecutionBinding
]


class CatxRuntimeAdapter:
    """Execute one fresh CATX session and reject unverified attachment drift."""

    id = "catx-online"

    def __init__(
        self,
        client: CatxAgentClient,
        binding_resolver: CatxBindingResolver,
        *,
        poll_interval_seconds: float = 1.0,
        max_wait_seconds: float = 900.0,
    ) -> None:
        if not callable(binding_resolver):
            raise TypeError("binding_resolver must be callable")
        if poll_interval_seconds <= 0 or max_wait_seconds <= 0:
            raise ValueError("CATX polling intervals must be positive")
        self.client = client
        self.binding_resolver = binding_resolver
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.max_wait_seconds = float(max_wait_seconds)

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities.from_values(
            (
                "fresh_session",
                "canonical_trace",
                "remote_agent",
                "repository_attachment",
                "skill_version_binding",
            )
        )

    async def execute(
        self,
        prepared: PreparedScenario,
        subject: SubjectSnapshot,
        context: RunContext,
    ) -> RuntimeResult:
        binding = self.binding_resolver(prepared, subject, context)
        if not isinstance(binding, CatxExecutionBinding):
            raise CatxBindingError("binding_resolver must return CatxExecutionBinding")
        expected_subject = subject.content_hash
        if not expected_subject.startswith("sha256:"):
            expected_subject = "sha256:" + expected_subject
        if binding.subject_hash != expected_subject:
            raise CatxBindingError("CATX binding subject_hash does not match SubjectSnapshot")
        title = str(prepared.metadata.get("title") or "aceval %s" % context.run_id)
        session_id = await asyncio.to_thread(
            self.client.start_session,
            {"prompt": prepared.prompt, "title": title},
            binding=binding,
        )
        budget_wait = (
            context.budget.max_wall_time_seconds
            if context.budget is not None
            else None
        )
        wait_limit = min(self.max_wait_seconds, budget_wait) if budget_wait else self.max_wait_seconds
        deadline = time.monotonic() + wait_limit
        status = None
        while time.monotonic() < deadline:
            status = await asyncio.to_thread(self.client.poll_session, session_id)
            if status.get("status") in ("COMPLETED", "FAILED"):
                break
            await asyncio.sleep(self.poll_interval_seconds)
        else:
            raise TimeoutError("CATX session did not reach terminal state within the run budget")

        evidence = await asyncio.to_thread(self.client.verify_binding, session_id, binding)
        evidence.assert_matches(binding)
        bundle = await asyncio.to_thread(self.client.fetch_session, session_id)
        result = bundle.to_runtime_result()
        metadata = dict(result.metadata)
        metadata.update(
            {
                "session_id": session_id,
                "catx_binding_hash": binding.binding_hash,
                "catx_binding_verified": True,
                "catx_actual_subject_hash": evidence.actual_subject_hash,
                "catx_actual_repository_hash": evidence.actual_repository_hash,
                "catx_actual_base_commit": evidence.actual_base_commit,
                "catx_actual_head_commit": evidence.actual_head_commit,
                "catx_binding_details": dict(evidence.details),
                "catx_terminal_status": dict(status or {}),
            }
        )
        return replace(result, metadata=metadata)


__all__ = ["CatxBindingResolver", "CatxRuntimeAdapter"]
