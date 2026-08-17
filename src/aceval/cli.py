"""Command-line interface for the reference EvalOps implementation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import shlex
import sys
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

from . import __version__
from .agent_runtime import CommandModelClient, ReferenceAgentRuntime, ReferenceRuntimeConfig
from .contracts import GradeStatus, RunBudget, SubjectSnapshot
from .optimizer import FrozenCandidateOptimizer, SkillMarkdownOptimizer
from .orchestrator import EvalOrchestrator
from .pack import EvalPackLoader, PackError
from .registry import build_builtin_registry
from .reporting import to_report_dict, write_report
from .runtime import FakeRuntime, ReferenceRuntimeAdapter


def _add_pack_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pack", required=True, help="Path to an EvalPack directory")


def _add_output_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-root",
        default=".aceval/runs",
        help="Directory for immutable run artifacts (default: .aceval/runs)",
    )


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runtime",
        choices=("fake", "reference"),
        required=True,
        help="Execution runtime. fake is simulation; reference requires --model-command.",
    )
    parser.add_argument(
        "--model-command",
        help="Quoted argv for a JSON stdin/stdout model bridge; no shell is used",
    )
    parser.add_argument(
        "--model-env",
        action="append",
        default=[],
        metavar="NAME",
        help="Environment variable allowlisted for the model bridge (repeatable)",
    )
    parser.add_argument("--model-timeout", type=float, default=30.0)
    parser.add_argument(
        "--model-id",
        help="Operator-declared model identifier recorded in the Runtime profile",
    )
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--max-tool-calls", type=int)


def _add_splits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--split",
        action="append",
        choices=("dev", "validation"),
        help="Suite split to run (repeatable; default: dev). Holdout is optimize-only.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aceval",
        description="Evaluate and iteratively improve Agent Skills on one reference runtime.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)

    pack = commands.add_parser("pack", help="Inspect an EvalPack")
    pack_commands = pack.add_subparsers(dest="pack_command", required=True)
    lint = pack_commands.add_parser("lint", help="Load and validate all Pack references")
    lint.add_argument("path", help="Path to an EvalPack directory")
    lint.set_defaults(handler=_pack_lint)
    test = pack_commands.add_parser(
        "test", help="Run visible-split conformance with the deterministic FakeRuntime"
    )
    test.add_argument("path", help="Path to an EvalPack directory")
    test.add_argument("--runtime", choices=("fake",), default="fake")
    _add_output_argument(test)
    test.set_defaults(handler=_pack_test)

    run = commands.add_parser("run", help="Evaluate one frozen Subject")
    _add_pack_argument(run)
    run.add_argument("--subject", required=True, help="Skill directory or SKILL.md")
    _add_splits(run)
    _add_runtime_arguments(run)
    _add_output_argument(run)
    run.set_defaults(handler=_run)

    compare = commands.add_parser("compare", help="Run a paired baseline/candidate comparison")
    _add_pack_argument(compare)
    compare.add_argument("--subject", required=True, help="Baseline Skill directory")
    compare.add_argument("--candidate", required=True, help="Candidate Skill directory")
    _add_splits(compare)
    _add_runtime_arguments(compare)
    _add_output_argument(compare)
    compare.set_defaults(handler=_compare)

    optimize = commands.add_parser(
        "optimize", help="Generate or load candidates and apply dev/validation/holdout gates"
    )
    _add_pack_argument(optimize)
    optimize.add_argument("--subject", required=True, help="Baseline Skill directory")
    optimize.add_argument(
        "--candidate",
        help="Frozen candidate directory for deterministic/offline optimization",
    )
    optimize.add_argument(
        "--optimizer-command",
        help="Separate JSON model bridge argv for candidate generation",
    )
    optimize.add_argument("--max-rounds", type=int)
    _add_runtime_arguments(optimize)
    _add_output_argument(optimize)
    optimize.set_defaults(handler=_optimize)
    return parser


def _splits(args: argparse.Namespace) -> Tuple[str, ...]:
    return tuple(args.split or ("dev",))


def _budget(args: argparse.Namespace) -> RunBudget:
    return RunBudget(
        max_total_tokens=getattr(args, "max_tokens", None),
        max_cost_usd=getattr(args, "max_cost_usd", None),
        max_tool_calls=getattr(args, "max_tool_calls", None),
    )


def _model_client(command: str, args: argparse.Namespace) -> CommandModelClient:
    argv = shlex.split(command)
    if not argv:
        raise ValueError("model command cannot be empty")
    return CommandModelClient(
        argv,
        timeout_seconds=args.model_timeout,
        env_allowlist=args.model_env,
        model_id=args.model_id,
    )


def _runtime(args: argparse.Namespace) -> Any:
    if args.runtime == "fake":
        return FakeRuntime()
    if not args.model_command:
        raise ValueError("--runtime reference requires --model-command")
    if (
        args.max_steps <= 0
        or not math.isfinite(args.model_timeout)
        or args.model_timeout <= 0
    ):
        raise ValueError("--max-steps and --model-timeout must be positive and finite")
    client = _model_client(args.model_command, args)
    engine = ReferenceAgentRuntime(
        client,
        ReferenceRuntimeConfig(
            max_steps=args.max_steps,
            max_tool_calls=args.max_tool_calls or 64,
        ),
    )
    return ReferenceRuntimeAdapter(engine)


def _load_pack(path: str) -> Tuple[Any, Any]:
    registry = build_builtin_registry()
    return registry, EvalPackLoader(registry).load(path)


def _print_summary(value: Any, report_paths: Optional[Tuple[Path, Path]] = None) -> None:
    report = to_report_dict(value, detailed=False)
    payload = {
        "schema_version": report.get("schema_version"),
        "report_type": report.get("report_type"),
        "simulated": report.get("simulated", False),
        "summary": report.get("summary", {}),
    }
    for key in ("accepted", "hard_regression_count"):
        if key in report:
            payload[key] = report[key]
    if hasattr(value, "run_id"):
        payload["run_id"] = value.run_id
    if report_paths:
        payload["report_json"] = str(report_paths[0])
        payload["report_markdown"] = str(report_paths[1])
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _pack_lint(args: argparse.Namespace) -> int:
    registry, pack = _load_pack(args.path)
    report = EvalPackLoader().validate(pack, registry)
    unresolved = []
    for split in ("validation", "holdout"):
        suite = pack.manifest.suite
        declared = getattr(suite, split) is not None or getattr(suite, split + "_ref") is not None
        if declared and not any(item.split == split for item in pack.scenarios):
            unresolved.append(
                {
                    "code": "unresolved_suite_ref",
                    "message": "%s suite needs an external resolver, which is not configured" % split,
                    "path": "manifest.suite.%s_ref" % split,
                    "severity": "error",
                }
            )
    payload = {
        "ok": report.ok and not unresolved,
        "pack": pack.manifest.metadata.name,
        "version": pack.manifest.metadata.version,
        "pack_hash": pack.pack_hash,
        "suite_hash": pack.suite_hash,
        "scenarios": len(pack.scenarios),
        "components": registry.component_ids(),
        "issues": [
            {
                "code": item.code,
                "message": item.message,
                "path": item.path,
                "severity": item.severity.value,
            }
            for item in report.issues
        ] + unresolved,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 2


def _pack_test(args: argparse.Namespace) -> int:
    registry, pack = _load_pack(args.path)
    runtime = FakeRuntime()
    content = "# EvalPack conformance subject\n"
    subject = SubjectSnapshot(
        kind=pack.manifest.subject_contract.kinds[0],
        uri=str(pack.root),
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        content=content,
        metadata={"variant": "baseline"},
    )
    orchestrator = EvalOrchestrator(registry, runtime, Path(args.output_root))
    available_splits = tuple(
        dict.fromkeys(
            scenario.split
            for scenario in pack.scenarios
            if scenario.split in ("dev", "validation")
        )
    )
    run = asyncio.run(
        orchestrator.evaluate(
            pack,
            subject,
            available_splits,
            run_label="pack-test",
        )
    )
    report_paths = write_report(run, run.run_dir)
    _print_summary(run, report_paths)
    invalid = any(
        item.status in (GradeStatus.ERROR, GradeStatus.NOT_EVALUABLE)
        for item in run.scenarios
    )
    return 2 if invalid else 0


def _run(args: argparse.Namespace) -> int:
    registry, pack = _load_pack(args.pack)
    orchestrator = EvalOrchestrator(
        registry, _runtime(args), Path(args.output_root), _budget(args)
    )
    result = asyncio.run(orchestrator.evaluate(pack, args.subject, _splits(args)))
    report_paths = write_report(result, result.run_dir)
    _print_summary(result, report_paths)
    return 0 if result.passed else 1


def _compare(args: argparse.Namespace) -> int:
    registry, pack = _load_pack(args.pack)
    orchestrator = EvalOrchestrator(
        registry, _runtime(args), Path(args.output_root), _budget(args)
    )
    result = asyncio.run(
        orchestrator.compare(pack, args.subject, args.candidate, _splits(args))
    )
    output_dir = Path(args.output_root).expanduser().resolve() / (
        "compare-%s" % result.candidate.run_id.rsplit("-", 1)[-1]
    )
    report_paths = write_report(result, output_dir)
    _print_summary(result, report_paths)
    if not result.evaluable:
        return 2
    return 0 if result.accepted else 1


def _optimize(args: argparse.Namespace) -> int:
    registry, pack = _load_pack(args.pack)
    runtime = _runtime(args)
    if args.runtime == "fake" and not args.candidate:
        raise ValueError(
            "FakeRuntime optimize requires --candidate and is simulation-only; use reference for live generation"
        )
    if args.candidate:
        optimizer = FrozenCandidateOptimizer(Path(args.candidate))
    else:
        command = args.optimizer_command or args.model_command
        if not command:
            raise ValueError(
                "optimize requires --candidate or a model bridge via --optimizer-command/--model-command"
            )
        optimizer = SkillMarkdownOptimizer(_model_client(command, args))
    orchestrator = EvalOrchestrator(registry, runtime, Path(args.output_root), _budget(args))
    result = asyncio.run(
        orchestrator.optimize(pack, args.subject, optimizer, max_rounds=args.max_rounds)
    )
    output_dir = result.output_dir or Path(args.output_root)
    report_paths = write_report(result, output_dir)
    _print_summary(result, report_paths)
    return 0 if result.accepted else 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (PackError, ValueError, OSError, RuntimeError) as exc:
        print("aceval: %s" % exc, file=sys.stderr)
        return 2


__all__ = ["build_parser", "main"]
