"""Explicit registration of the trusted components shipped with aceval."""

from __future__ import annotations

from typing import Any, Optional

from .drivers import register_builtin_drivers
from .graders import register_builtin_graders
from .optimizer import SkillOptimizerBridge
from .pack import ComponentRegistry
from .subjects import SkillMarkdownSubjectAdapter


def build_builtin_registry(optimizer: Optional[Any] = None) -> ComponentRegistry:
    """Create a fresh Registry without importing code named by an EvalPack."""

    registry = ComponentRegistry(subject_kinds=("skill",))
    subject_adapter = SkillMarkdownSubjectAdapter()
    registry.register_subject_adapter(
        subject_adapter.id, subject_adapter, kinds=("skill",)
    )
    register_builtin_drivers(registry)
    register_builtin_graders(registry)
    # Pack lint validates that this optimizer capability is known. A concrete
    # model-bound instance is supplied explicitly to EvalOrchestrator.optimize.
    registry.register_optimizer(
        "skill_markdown_v1", optimizer if optimizer is not None else SkillOptimizerBridge
    )
    return registry


__all__ = ["build_builtin_registry"]
