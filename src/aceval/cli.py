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
from typing import Any, Mapping, Optional, Sequence, Tuple

from . import __version__
from .agent_runtime import CommandModelClient, ReferenceAgentRuntime, ReferenceRuntimeConfig
from .connections import (
    CompanyApiProfile,
    HTTPSessionLogProvider,
    IMPORTED_SESSION_API_VERSION,
    ImportedRunBundle,
    load_imported_run_bundle,
    load_session_log,
)
from .contracts import GradeStatus, RunBudget, SubjectSnapshot, as_primitive
from .failure_attribution import FailureAttributor
from .optimizer import FrozenCandidateOptimizer, SkillMarkdownOptimizer
from .orchestrator import EvalOrchestrator
from .pack import EvalPackLoader, PackError
from .pack_builder import (
    PACK_TYPES,
    begin_calibration,
    freeze_evalpack,
    generate_evalpack,
)
from .pack_lifecycle import pack_calibration_status
from .pack_quality import evaluate_pack_quality
from .planning_workflow import (
    REFERENCE_RUNTIME_CAPABILITIES,
    create_planning_artifacts,
    load_planning_artifacts,
)
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

    plan = commands.add_parser(
        "plan",
        help="Analyze a Skill and generate a source-grounded Test Plan",
    )
    plan.add_argument("--subject", required=True, help="Skill directory or SKILL.md")
    plan.add_argument("--cases", required=True, help="Seed cases JSON file")
    plan.add_argument("--goal", required=True)
    plan.add_argument("--output", required=True, help="New planning artifact directory")
    plan.add_argument(
        "--runtime-profile",
        choices=("reference", "unknown"),
        default="reference",
        help="Capabilities used for feasibility routing",
    )
    plan.add_argument(
        "--runtime-capability",
        action="append",
        default=[],
        metavar="NAME",
        help="Additional/explicit Runtime capability (repeatable)",
    )
    plan.add_argument("--max-generated-cases", type=int, default=12)
    plan.set_defaults(handler=_plan)

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
    generate = pack_commands.add_parser(
        "generate",
        help="Generate an untrusted draft Pack from a few cases and a goal",
    )
    generate.add_argument("--type", dest="pack_type")
    generate.add_argument("--cases", help="Cases JSON file")
    generate.add_argument(
        "--plan",
        help="Planning artifact directory produced by `aceval plan`",
    )
    generate.add_argument("--goal")
    generate.add_argument("--output", required=True, help="New Pack directory")
    generate.add_argument(
        "--objective",
        help="Optional JSON object or @FILE; measurable efficiency goals are inferred when omitted",
    )
    generate.add_argument("--name")
    generate.add_argument("--version")
    generate.add_argument("--description")
    generate.add_argument("--source-root")
    generate.set_defaults(handler=_pack_generate)
    calibrate = pack_commands.add_parser(
        "calibrate", help="Move a generated draft into the editable calibration stage"
    )
    calibrate.add_argument("path")
    calibrate.set_defaults(handler=_pack_calibrate)
    freeze = pack_commands.add_parser(
        "freeze", help="Explicitly approve and lock a calibrated generated Pack"
    )
    freeze.add_argument("path")
    freeze.add_argument(
        "--approve",
        action="store_true",
        help="Confirm that cases, Oracles, Graders, and objective were reviewed",
    )
    freeze.set_defaults(handler=_pack_freeze)
    quality = pack_commands.add_parser(
        "quality",
        help="Evaluate test-design coverage, Oracle trust, and freeze blockers",
    )
    quality.add_argument("path")
    quality.set_defaults(handler=_pack_quality)

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

    doctor = commands.add_parser(
        "doctor",
        help="Generate/freeze a Pack when needed, then auto-select repair or tune",
    )
    doctor.add_argument("--subject", required=True, help="Skill directory or SKILL.md")
    doctor.add_argument("--pack", help="Use an existing Pack instead of generating one")
    doctor.add_argument("--cases", help="Cases JSON used when --pack is omitted")
    doctor.add_argument("--type", dest="pack_type", default="generic")
    doctor.add_argument("--goal", help="Natural-language repair/tuning goal")
    doctor.add_argument("--pack-output", help="New generated Pack directory")
    doctor.add_argument("--objective", help="Optional JSON object or @FILE")
    doctor.add_argument("--source-root")
    doctor.add_argument(
        "--auto-plan",
        action="store_true",
        help="Analyze the Skill and generate a Test Plan before building the Pack",
    )
    doctor.add_argument("--plan-output", help="New planning artifact directory")
    doctor.add_argument("--max-generated-cases", type=int, default=12)
    doctor.add_argument(
        "--allow-subject-drift",
        action="store_true",
        help="Explicitly allow a plan-generated Pack to run against a different Subject hash",
    )
    doctor.add_argument(
        "--approve-pack",
        action="store_true",
        help="Approve a supported generated template after reviewing its cases/Oracles",
    )
    doctor.add_argument("--candidate", help="Frozen candidate for simulation/offline runs")
    doctor.add_argument("--optimizer-command")
    doctor.add_argument("--max-rounds", type=int)
    _add_runtime_arguments(doctor)
    _add_output_argument(doctor)
    doctor.set_defaults(handler=_doctor)

    profile = commands.add_parser("profile", help="Manage company Agent API profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_validate = profile_commands.add_parser(
        "validate", help="Validate a profile without reading its secret environment value"
    )
    profile_validate.add_argument("path")
    profile_validate.set_defaults(handler=_profile_validate)

    session = commands.add_parser("session", help="Fetch or import company Agent sessions")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_fetch = session_commands.add_parser(
        "fetch", help="Fetch and normalize one company Agent session log"
    )
    session_fetch.add_argument("--profile", required=True)
    session_fetch.add_argument("--session-id", required=True)
    session_fetch.add_argument("--output", required=True)
    session_fetch.add_argument("--force", action="store_true")
    session_fetch.set_defaults(handler=_session_fetch)
    session_import = session_commands.add_parser(
        "import", help="Normalize a previously downloaded session JSON file"
    )
    session_import.add_argument("--profile", required=True)
    session_import.add_argument("--session-id", required=True)
    session_import.add_argument("--input", required=True)
    session_import.add_argument("--output", required=True)
    session_import.add_argument("--force", action="store_true")
    session_import.set_defaults(handler=_session_import)
    session_execute = session_commands.add_parser(
        "execute", help="Submit a request through the profile's reserved execute endpoint"
    )
    session_execute.add_argument("--profile", required=True)
    session_execute.add_argument("--request", required=True, help="JSON file")
    session_execute.set_defaults(handler=_session_execute)
    session_diagnose = session_commands.add_parser(
        "diagnose",
        help="Diagnose execution/tool failures in a normalized imported session",
    )
    session_diagnose.add_argument("--input", required=True)
    session_diagnose.add_argument("--output")
    session_diagnose.add_argument("--force", action="store_true")
    session_diagnose.set_defaults(handler=_session_diagnose)
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


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant: %s" % value)


def _unique_json_object(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _json_object(value: str, label: str) -> Mapping[str, Any]:
    source = value
    if value.startswith("@"):
        try:
            source = Path(value[1:]).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError("cannot read %s JSON file: %s" % (label, exc)) from exc
    try:
        payload = json.loads(
            source,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("%s must be a strict JSON object" % label) from exc
    if not isinstance(payload, Mapping):
        raise ValueError("%s must be a JSON object" % label)
    return payload


def _pack_state_payload(result: Any) -> Mapping[str, Any]:
    return {
        "ok": True,
        "pack": str(result.root),
        "manifest": str(result.manifest_path),
        "requested_type": result.requested_type or result.pack_type,
        "template": result.pack_type,
        "calibration_status": result.calibration_status,
        "trusted": result.trusted,
        "pack_hash": result.pack_hash,
        "optimization_eligible": result.trusted,
    }


def _plan(args: argparse.Namespace) -> int:
    if args.max_generated_cases < 0:
        raise ValueError("--max-generated-cases must be non-negative")
    if args.runtime_profile == "unknown" and not args.runtime_capability:
        capabilities = None
    else:
        capabilities = set(args.runtime_capability)
        if args.runtime_profile == "reference":
            capabilities.update(REFERENCE_RUNTIME_CAPABILITIES)
    artifacts = create_planning_artifacts(
        args.subject,
        args.cases,
        args.goal,
        args.output,
        runtime_capabilities=(
            tuple(sorted(capabilities)) if capabilities is not None else None
        ),
        max_generated_cases=args.max_generated_cases,
    )
    payload = dict(artifacts.summary())
    payload.update(
        {
            "ok": True,
            "files": {
                "capability_graph": str(artifacts.root / "capability-graph.json"),
                "test_plan": str(artifacts.root / "test-plan.json"),
                "case_drafts": str(artifacts.root / "case-drafts.json"),
                "coverage": str(artifacts.root / "coverage.json"),
                "runtime_gaps": str(artifacts.root / "runtime-gaps.json"),
            },
            "freeze_blockers": [
                item.to_dict() for item in artifacts.freeze_blockers
            ],
            "next_step": (
                "Review pending Oracles and blockers, then run `aceval pack generate --plan ...`."
            ),
        }
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _pack_generate(args: argparse.Namespace) -> int:
    objective = _json_object(args.objective, "objective") if args.objective else None
    if args.plan:
        if args.cases:
            raise ValueError("--plan and --cases are mutually exclusive")
        planned = load_planning_artifacts(args.plan)
        cases = planned.case_generation.cases_document
        pack_type = args.pack_type or "generic"
        if args.goal is not None and args.goal != planned.test_plan.goal:
            raise ValueError(
                "--goal does not match the Test Plan; re-run `aceval plan` "
                "so requirements and risk weights are derived from the new goal"
            )
        goal = planned.test_plan.goal
        test_design = planned.test_design()
        source_root = args.source_root or (
            str(planned.seed_source_root)
            if planned.seed_source_root is not None
            else None
        )
    else:
        if not args.cases or not args.pack_type or not args.goal:
            raise ValueError(
                "pack generate requires --type, --cases, and --goal unless --plan is used"
            )
        cases = args.cases
        pack_type = args.pack_type
        goal = args.goal
        test_design = None
        source_root = args.source_root
    result = generate_evalpack(
        cases,
        pack_type,
        goal,
        args.output,
        objective=objective,
        name=args.name,
        version=args.version,
        description=args.description,
        source_root=source_root,
        test_design=test_design,
    )
    payload = dict(_pack_state_payload(result))
    payload["next_step"] = (
        "Review the generated cases, Oracles, Graders, and objective; then run "
        "`aceval pack calibrate` / `aceval pack freeze --approve`."
    )
    if args.plan:
        payload["test_plan"] = str(Path(args.plan).expanduser().resolve())
        payload["pack_quality"] = evaluate_pack_quality(result.root).to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _pack_calibrate(args: argparse.Namespace) -> int:
    result = begin_calibration(args.path)
    payload = dict(_pack_state_payload(result))
    payload["next_step"] = (
        "Iterate only the EvalPack until its evaluator is trusted, then freeze it."
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _pack_freeze(args: argparse.Namespace) -> int:
    result = freeze_evalpack(args.path, approve=bool(args.approve))
    print(
        json.dumps(
            _pack_state_payload(result),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _pack_quality(args: argparse.Namespace) -> int:
    report = evaluate_pack_quality(args.path)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.ready_for_freeze else 2


def _pack_lint(args: argparse.Namespace) -> int:
    registry, pack = _load_pack(args.path)
    report = EvalPackLoader().validate(pack, registry)
    lifecycle = pack_calibration_status(pack)
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
        "calibration_status": lifecycle,
        "optimization_eligible": lifecycle in ("frozen", "legacy"),
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


def _doctor(args: argparse.Namespace) -> int:
    pack_path = args.pack
    if pack_path is None:
        if not args.cases or not args.goal:
            raise ValueError("doctor requires --cases and --goal when --pack is omitted")
        output = args.pack_output
        if output is None:
            output = str(Path(".aceval/packs") / (Path(args.cases).stem + "-pack"))
        objective = (
            _json_object(args.objective, "objective") if args.objective else None
        )
        planned = None
        if args.auto_plan:
            if args.max_generated_cases < 0:
                raise ValueError("--max-generated-cases must be non-negative")
            plan_output = args.plan_output or (str(output) + ".plan")
            planned = create_planning_artifacts(
                args.subject,
                args.cases,
                args.goal,
                plan_output,
                runtime_capabilities=tuple(
                    sorted(REFERENCE_RUNTIME_CAPABILITIES)
                ),
                max_generated_cases=args.max_generated_cases,
            )
            cases = planned.case_generation.cases_document
            source_root = args.source_root or (
                str(planned.seed_source_root)
                if planned.seed_source_root is not None
                else None
            )
            test_design = planned.test_design()
        else:
            cases = args.cases
            source_root = args.source_root
            test_design = None
        generated = generate_evalpack(
            cases,
            args.pack_type,
            args.goal,
            output,
            objective=objective,
            source_root=source_root,
            test_design=test_design,
        )
        pack_path = str(generated.root)
        if planned is not None:
            quality = evaluate_pack_quality(generated.root)
            if not quality.ready_for_freeze:
                calibrating = begin_calibration(generated.root)
                payload = dict(_pack_state_payload(calibrating))
                payload.update(
                    {
                        "status": "calibration_required",
                        "reason": (
                            "The generated Test Plan or Pack Quality report has blockers. "
                            "Resolve them before an explicit freeze."
                        ),
                        "planning": planned.summary(),
                        "pack_quality": quality.to_dict(),
                    }
                )
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
                return 0
        if generated.requested_type not in PACK_TYPES:
            calibrating = begin_calibration(generated.root)
            payload = dict(_pack_state_payload(calibrating))
            payload.update(
                {
                    "status": "calibration_required",
                    "reason": (
                        "The requested type used the generic fallback. Review and "
                        "calibrate its evaluator before a separate explicit freeze."
                    ),
                }
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if not args.approve_pack:
            calibrating = begin_calibration(generated.root)
            payload = dict(_pack_state_payload(calibrating))
            payload.update(
                {
                    "status": "calibration_required",
                    "reason": (
                        "Generated Pack is not allowed to optimize the Skill until "
                        "you explicitly approve its evaluation contract."
                    ),
                }
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        freeze_evalpack(generated.root, approve=True)
    else:
        _, loaded = _load_pack(pack_path)
        lifecycle = pack_calibration_status(loaded)
        if lifecycle in ("draft", "calibrating"):
            if not args.approve_pack:
                payload = {
                    "ok": True,
                    "pack": str(loaded.root),
                    "calibration_status": lifecycle,
                    "optimization_eligible": False,
                    "status": "calibration_required",
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
                return 0
            freeze_evalpack(loaded.root, approve=True)

    registry, pack = _load_pack(str(pack_path))
    _verify_planned_subject(pack, args.subject, registry, bool(args.allow_subject_drift))
    runtime = _runtime(args)
    if args.runtime == "fake" and not args.candidate:
        raise ValueError(
            "FakeRuntime doctor requires --candidate and is simulation-only"
        )
    if args.candidate:
        optimizer = FrozenCandidateOptimizer(Path(args.candidate))
    else:
        command = args.optimizer_command or args.model_command
        if not command:
            raise ValueError(
                "doctor requires --candidate or a model bridge via "
                "--optimizer-command/--model-command"
            )
        optimizer = SkillMarkdownOptimizer(_model_client(command, args))
    orchestrator = EvalOrchestrator(
        registry, runtime, Path(args.output_root), _budget(args)
    )
    result = asyncio.run(
        orchestrator.optimize(
            pack, args.subject, optimizer, max_rounds=args.max_rounds
        )
    )
    output_dir = result.output_dir or Path(args.output_root)
    report_paths = write_report(result, output_dir)
    _print_summary(result, report_paths)
    return 0 if result.accepted else 2


def _verify_planned_subject(
    pack: Any,
    subject_ref: str,
    registry: Any,
    allow_subject_drift: bool,
) -> None:
    test_design = pack.manifest.metadata.extra.get("test_design")
    if not isinstance(test_design, Mapping):
        return
    expected = test_design.get("source_subject_hash")
    if not isinstance(expected, str) or not expected:
        raise ValueError("planned Pack is missing source_subject_hash")
    adapter = registry.subject_adapter(pack.manifest.subject_contract.adapter)
    snapshot = adapter.snapshot(
        subject_ref, pack.manifest.subject_contract.params
    )
    actual = str(snapshot.content_hash)
    if not actual.startswith("sha256:"):
        actual = "sha256:" + actual
    if actual != expected and not allow_subject_drift:
        raise ValueError(
            "planned Pack source Subject hash does not match --subject; "
            "re-run `aceval plan` or pass --allow-subject-drift explicitly"
        )


def _profile_validate(args: argparse.Namespace) -> int:
    profile = CompanyApiProfile.load(args.path)
    payload = {
        "ok": True,
        "api_version": profile.api_version,
        "name": profile.name,
        "base_url": profile.base_url,
        "auth": {
            "type": profile.auth.type,
            "env": profile.auth.env,
            "header": profile.auth.header,
        },
        "execute_configured": profile.execute is not None,
        "session_log": as_primitive(profile.session_log),
        "secret_loaded": False,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _imported_bundle_payload(bundle: ImportedRunBundle) -> Mapping[str, Any]:
    return {
        "schema_version": IMPORTED_SESSION_API_VERSION,
        "session_id": bundle.session_id,
        "profile_name": bundle.profile_name,
        "source": bundle.source,
        "completeness": bundle.completeness.as_dict(),
        "observation": as_primitive(bundle.observation),
    }


def _write_json_output(path_value: str, payload: Mapping[str, Any], force: bool) -> Path:
    requested = Path(path_value).expanduser()
    if requested.is_symlink():
        raise ValueError("output cannot be a symlink: %s" % requested)
    path = requested.absolute()
    if path.exists() and not force:
        raise ValueError("output already exists; pass --force to replace it: %s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    return path


def _session_fetch(args: argparse.Namespace) -> int:
    profile = CompanyApiProfile.load(args.profile)
    bundle = HTTPSessionLogProvider(profile).fetch_session(args.session_id)
    output = _write_json_output(
        args.output, _imported_bundle_payload(bundle), bool(args.force)
    )
    print(
        json.dumps(
            {
                "ok": True,
                "session_id": bundle.session_id,
                "profile": bundle.profile_name,
                "complete": bundle.completeness.complete,
                "output": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _session_import(args: argparse.Namespace) -> int:
    profile = CompanyApiProfile.load(args.profile)
    bundle = load_session_log(profile, args.session_id, args.input)
    output = _write_json_output(
        args.output, _imported_bundle_payload(bundle), bool(args.force)
    )
    print(
        json.dumps(
            {
                "ok": True,
                "session_id": bundle.session_id,
                "profile": bundle.profile_name,
                "complete": bundle.completeness.complete,
                "output": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _session_execute(args: argparse.Namespace) -> int:
    profile = CompanyApiProfile.load(args.profile)
    request = _json_object("@" + args.request, "request")
    session_id = HTTPSessionLogProvider(profile).execute(request)
    print(
        json.dumps(
            {"ok": True, "profile": profile.name, "session_id": session_id},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _session_diagnose(args: argparse.Namespace) -> int:
    bundle = load_imported_run_bundle(args.input)
    report = FailureAttributor().attribute(
        scenario_id="session-%s" % bundle.session_id,
        observation=bundle.observation,
        error=bundle.observation.error,
        metadata={"missing_observation_fields": bundle.completeness.missing},
    )
    payload = {
        "ok": True,
        "schema_version": "aceval.session-diagnosis/v1",
        "session_id": bundle.session_id,
        "profile_name": bundle.profile_name,
        "completeness": bundle.completeness.as_dict(),
        "diagnostic_report": report.to_dict(),
    }
    if args.output:
        output = _write_json_output(args.output, payload, bool(args.force))
        payload["output"] = str(output)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.patch_decision.value != "needs_more_evidence" else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (PackError, ValueError, OSError, RuntimeError) as exc:
        print("aceval: %s" % exc, file=sys.stderr)
        return 2


__all__ = ["build_parser", "main"]
