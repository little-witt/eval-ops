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
from .console import build_console, serve_console
from .d2c import (
    D2C_PROFILE_API_VERSION,
    D2CBrowserProfile,
    D2CValidationRequest,
    check_d2c_profile,
    validate_d2c,
)
from .catx import (
    CATX_PROFILE_API_VERSION,
    CatxAgentClient,
    CatxAgentProfile,
)
from .contracts import GradeStatus, Oracle, RunBudget, SubjectSnapshot, as_primitive
from .code_review import (
    CodeReviewFindingsGrader,
    DEFAULT_CODE_REVIEW_CASE,
    DEFAULT_CODE_REVIEW_STACKS,
    create_code_review_fixture_lab,
    infer_code_review_stacks,
    repository_observation,
    repository_verify_blueprint,
)
from .environment_contracts import (
    CANDIDATE_BUNDLE_API_VERSION,
    VALIDATION_REQUEST_API_VERSION,
    CandidateBundle,
    EnvironmentBlueprint,
    ValidationRequest,
    ValidationStatus,
    create_candidate_bundle,
    write_contract,
)
from .environments import LocalDockerProvider, build_repository_verify_image
from .divergence_predictor import analyze_divergence
from .execution_path import ExecutionPathSpec, evaluate_trace_conformance
from .iteration_kernel import IterationKernel
from .kernel_contracts import KernelConfig, KernelInput
from .failure_attribution import FailureAttributor
from .experiments import (
    ExperimentPlanError,
    resolve_experiment_plan,
)
from .evaluation_compiler import (
    compile_evaluation,
    default_output_path,
)
from .optimizer import FrozenCandidateOptimizer, SkillMarkdownOptimizer
from .online_code_review import run_online_review_suite, write_online_review_benchmark
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
from .task_center import TaskStore


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
        help="Confirm that cases, Oracles, and Graders were reviewed",
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
    optimize.add_argument(
        "--experiment",
        help="ExperimentPlan JSON; defaults to <pack>.experiment.json or legacy policy",
    )
    _add_runtime_arguments(optimize)
    _add_output_argument(optimize)
    optimize.set_defaults(handler=_optimize)

    doctor = commands.add_parser(
        "doctor",
        help="Generate/freeze a Pack when needed, then auto-select repair or tune",
    )
    doctor.add_argument("--subject", required=True, help="Skill directory or SKILL.md")
    doctor.add_argument("--pack", help="Use an existing Pack instead of generating one")
    doctor.add_argument(
        "--cases",
        help="Optional Cases JSON override; otherwise a minimal evaluation is compiled",
    )
    doctor.add_argument(
        "--standards",
        help="Acceptance standards as text or @FILE; defaults to --goal",
    )
    doctor.add_argument(
        "--prompt",
        help="Optional task prompt paired with --expected-output when Cases are omitted",
    )
    doctor.add_argument(
        "--expected-output",
        help="Expected JSON value or @FILE for the generated task",
    )
    doctor.add_argument("--type", dest="pack_type", default="auto")
    doctor.add_argument("--goal", help="Natural-language repair/tuning goal")
    doctor.add_argument("--pack-output", help="New generated Pack directory")
    doctor.add_argument("--objective", help="Optional JSON object or @FILE")
    doctor.add_argument("--source-root")
    doctor.add_argument(
        "--suite-root",
        action="append",
        default=[],
        help="Additional EvalSuite search root (repeatable)",
    )
    doctor.add_argument(
        "--no-reuse",
        action="store_true",
        help="Disable exact EvalSuite reuse for this run",
    )
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
        "--approve-evaluation",
        dest="approve_pack",
        action="store_true",
        help="Approve the generated evaluation contract after reviewing expectations",
    )
    doctor.add_argument("--candidate", help="Frozen candidate for simulation/offline runs")
    doctor.add_argument("--optimizer-command")
    doctor.add_argument("--max-rounds", type=int)
    doctor.add_argument(
        "--experiment",
        help="ExperimentPlan JSON; defaults to <pack>.experiment.json or legacy policy",
    )
    _add_runtime_arguments(doctor)
    _add_output_argument(doctor)
    doctor.set_defaults(handler=_doctor)

    code_review = commands.add_parser(
        "code-review", help="Prepare and grade deterministic code-review Skill fixtures"
    )
    code_review_commands = code_review.add_subparsers(
        dest="code_review_command", required=True
    )
    code_review_init = code_review_commands.add_parser(
        "init-lab", help="Create isolated Git repositories covering P0 review cases"
    )
    code_review_init.add_argument("--output", required=True)
    code_review_init.add_argument("--force", action="store_true")
    code_review_init.add_argument(
        "--stack",
        action="append",
        choices=DEFAULT_CODE_REVIEW_STACKS,
        help="Stack to include; repeat as needed (default: all supported stacks)",
    )
    code_review_init.set_defaults(handler=_code_review_init_lab)
    code_review_select = code_review_commands.add_parser(
        "select-cases",
        help="Select stack-specific branches from one Fixture Lab repository",
    )
    code_review_select.add_argument("--lab", required=True)
    code_review_select.add_argument(
        "--skill",
        help="Skill directory or instruction file used for automatic stack inference",
    )
    code_review_select.add_argument(
        "--stack",
        action="append",
        choices=DEFAULT_CODE_REVIEW_STACKS,
        help="Explicit stack override; repeat as needed",
    )
    code_review_select.set_defaults(handler=_code_review_select_cases)
    code_review_grade = code_review_commands.add_parser(
        "grade", help="Grade one strict-JSON review result against an Oracle"
    )
    code_review_grade.add_argument("--repository", required=True)
    code_review_grade.add_argument("--findings", required=True, help="JSON or @FILE")
    code_review_grade.add_argument("--oracle", required=True, help="JSON or @FILE")
    code_review_grade.set_defaults(handler=_code_review_grade)
    code_review_online = code_review_commands.add_parser(
        "online-run", help="Run frozen code-review cases through fresh CATX sessions"
    )
    code_review_online.add_argument("--profile", required=True)
    code_review_online.add_argument("--lab", required=True)
    code_review_online.add_argument("--skill-ref", required=True)
    code_review_online.add_argument("--skill-commit", required=True)
    code_review_online.add_argument("--workspace", required=True)
    code_review_online.add_argument(
        "--configuration", choices=("old_skill", "with_skill"), required=True
    )
    code_review_online.add_argument(
        "--stack", action="append", choices=DEFAULT_CODE_REVIEW_STACKS
    )
    code_review_online.add_argument("--case", action="append", default=[])
    code_review_online.add_argument("--task-root", default=".aceval/tasks")
    code_review_online.add_argument("--task-id")
    code_review_online.add_argument("--iteration", type=int, default=0)
    code_review_online.add_argument(
        "--execution-path", help="ExecutionPathSpec JSON used as a trace-conformance dimension"
    )
    code_review_online.set_defaults(handler=_code_review_online_run)
    code_review_report = code_review_commands.add_parser(
        "online-report", help="Aggregate candidate/baseline CATX runs with exact usage"
    )
    code_review_report.add_argument("--workspace", required=True)
    code_review_report.add_argument(
        "--skill-name", default="frontend-code-reviewer"
    )
    code_review_report.add_argument("--skill-path", default="")
    code_review_report.set_defaults(handler=_code_review_online_report)

    console = commands.add_parser(
        "console", help="Build or serve the local Skill Optimization Cockpit"
    )
    console_commands = console.add_subparsers(dest="console_command", required=True)
    console_build = console_commands.add_parser(
        "build", help="Build a static offline optimization console"
    )
    console_build.add_argument("--workspace", required=True)
    console_build.add_argument("--output", required=True)
    console_build.add_argument("--plan")
    console_build.add_argument("--profile")
    console_build.add_argument("--task-root")
    console_build.set_defaults(handler=_console_build)
    console_serve = console_commands.add_parser(
        "serve", help="Serve a live read-only console with SSE updates"
    )
    console_serve.add_argument("--workspace")
    console_serve.add_argument("--plan")
    console_serve.add_argument("--profile")
    console_serve.add_argument("--task-root", default=".aceval/tasks")
    console_serve.add_argument("--host", default="127.0.0.1")
    console_serve.add_argument("--port", type=int, default=8765)
    console_serve.set_defaults(handler=_console_serve)

    d2c = commands.add_parser(
        "d2c", help="Check and run deterministic local-browser validation"
    )
    d2c_commands = d2c.add_subparsers(dest="d2c_command", required=True)
    d2c_check = d2c_commands.add_parser(
        "check", help="Check Chrome, Node, driver, and environment fingerprint"
    )
    d2c_check.add_argument("--profile", required=True)
    d2c_check.set_defaults(handler=_d2c_check)
    d2c_validate = d2c_commands.add_parser(
        "validate", help="Capture deterministic browser evidence for one D2C case"
    )
    d2c_validate.add_argument("--profile", required=True)
    d2c_validate.add_argument("--request", required=True)
    d2c_validate.add_argument("--output", required=True)
    d2c_validate.add_argument("--task-root", default=".aceval/tasks")
    d2c_validate.add_argument("--task-id")
    d2c_validate.add_argument("--iteration", type=int, default=0)
    d2c_validate.set_defaults(handler=_d2c_validate)

    kernel = commands.add_parser("kernel", help="Run the recoverable low-token Skill evaluation/iteration workflow")
    kernel_commands = kernel.add_subparsers(dest="kernel_command", required=True)
    kernel_create = kernel_commands.add_parser("create", help="Create a Kernel task from configuration and user intent")
    kernel_create.add_argument("--config", required=True)
    kernel_create.add_argument("--input", required=True)
    kernel_create.add_argument("--task-root", default=".aceval/tasks")
    kernel_create.add_argument("--id")
    kernel_create.set_defaults(handler=_kernel_create)
    kernel_status = kernel_commands.add_parser("status", help="Read one Kernel task state")
    kernel_status.add_argument("task_id")
    kernel_status.add_argument("--task-root", default=".aceval/tasks")
    kernel_status.set_defaults(handler=_kernel_status)
    kernel_log = kernel_commands.add_parser("log", help="Read one complete persisted remote session log")
    kernel_log.add_argument("task_id")
    kernel_log.add_argument("--task-root", default=".aceval/tasks")
    kernel_log.add_argument("--iteration", type=int, required=True)
    kernel_log.add_argument("--purpose", choices=("evaluation", "pass-verification", "without-skill-baseline"), required=True)
    kernel_log.add_argument("--case", required=True)
    kernel_log.set_defaults(handler=_kernel_log)
    kernel_compile = kernel_commands.add_parser("compile", help="Generate/reuse EvalPack, cases, and execution paths")
    kernel_compile.add_argument("task_id")
    kernel_compile.add_argument("--task-root", default=".aceval/tasks")
    kernel_compile.set_defaults(handler=_kernel_compile)
    kernel_dispatch = kernel_commands.add_parser("dispatch", help="Create all selected remote evaluation sessions")
    kernel_dispatch.add_argument("task_id")
    kernel_dispatch.add_argument("--task-root", default=".aceval/tasks")
    kernel_dispatch.add_argument("--purpose", default="evaluation")
    kernel_dispatch.add_argument("--case", action="append", default=[])
    kernel_dispatch.set_defaults(handler=_kernel_dispatch)
    kernel_collect = kernel_commands.add_parser("collect", help="Poll and recover complete logs for an active batch")
    kernel_collect.add_argument("task_id")
    kernel_collect.add_argument("--task-root", default=".aceval/tasks")
    kernel_collect.add_argument("--wait", action="store_true")
    kernel_collect.set_defaults(handler=_kernel_collect)
    kernel_analyze = kernel_commands.add_parser("analyze", help="Run one token-bounded cross-case local analysis")
    kernel_analyze.add_argument("task_id")
    kernel_analyze.add_argument("--task-root", default=".aceval/tasks")
    kernel_analyze.set_defaults(handler=_kernel_analyze)
    kernel_advance = kernel_commands.add_parser("advance", help="Perform the next automatic step and stop at a gate")
    kernel_advance.add_argument("task_id")
    kernel_advance.add_argument("--task-root", default=".aceval/tasks")
    kernel_advance.add_argument("--wait", action="store_true")
    kernel_advance.set_defaults(handler=_kernel_advance)
    kernel_run = kernel_commands.add_parser("run", help="Run automatically until confirmation, evidence, or convergence")
    kernel_run.add_argument("task_id")
    kernel_run.add_argument("--task-root", default=".aceval/tasks")
    kernel_run.add_argument("--max-steps", type=int, default=200)
    kernel_run.set_defaults(handler=_kernel_run)
    kernel_confirm = kernel_commands.add_parser("confirm", help="Approve optimization scope, reject it, or supplement intent")
    kernel_confirm.add_argument("task_id")
    kernel_confirm.add_argument("--task-root", default=".aceval/tasks")
    kernel_confirm_choice = kernel_confirm.add_mutually_exclusive_group(required=True)
    kernel_confirm_choice.add_argument("--approve", action="store_true")
    kernel_confirm_choice.add_argument("--reject", action="store_true")
    kernel_confirm_choice.add_argument("--supplement", help="Replacement KernelInput JSON")
    kernel_confirm.add_argument("--select-capability", action="append", default=[], help="Capability proposal id to approve; repeat for multiple discovery proposals")
    kernel_confirm.add_argument("--select-change", action="append", default=[], help="Optimization proposal id to approve; repeat for multiple proposals")
    kernel_confirm.add_argument("--feedback", help="Additional optimization guidance supplied at the approval gate")
    kernel_confirm.set_defaults(handler=_kernel_confirm)
    kernel_optimize = kernel_commands.add_parser("optimize", help="Generate a constrained candidate after approval")
    kernel_optimize.add_argument("task_id")
    kernel_optimize.add_argument("--task-root", default=".aceval/tasks")
    kernel_optimize.set_defaults(handler=_kernel_optimize)
    kernel_publish = kernel_commands.add_parser("publish", help="Commit/push an approved candidate and start its iteration")
    kernel_publish.add_argument("task_id")
    kernel_publish.add_argument("--task-root", default=".aceval/tasks")
    kernel_publish.set_defaults(handler=_kernel_publish)

    task = commands.add_parser("task", help="Create and inspect Skill evaluation tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_create = task_commands.add_parser(
        "create", help="Create a task with automatic or custom evaluation design"
    )
    task_create.add_argument("--root", default=".aceval/tasks")
    task_create.add_argument("--id")
    task_create.add_argument("--skill-name", required=True)
    task_create.add_argument("--skill-source", required=True)
    task_create.add_argument("--scenario", required=True)
    task_create.add_argument("--goal", required=True)
    task_create.add_argument("--standard", action="append", required=True)
    task_create.add_argument("--cases", help="Optional JSON array of user-provided cases")
    task_create.add_argument("--evaluation-spec", help="Optional custom evaluation JSON object")
    task_create.add_argument("--environment", help="Optional environment binding JSON object")
    task_create.set_defaults(handler=_task_create)
    task_list = task_commands.add_parser("list", help="List all evaluation tasks")
    task_list.add_argument("--root", default=".aceval/tasks")
    task_list.set_defaults(handler=_task_list)
    task_show = task_commands.add_parser("show", help="Show one task and its event timeline")
    task_show.add_argument("task_id")
    task_show.add_argument("--root", default=".aceval/tasks")
    task_show.set_defaults(handler=_task_show)
    task_event = task_commands.add_parser("event", help="Append a typed automatic iteration event")
    task_event.add_argument("task_id")
    task_event.add_argument("--root", default=".aceval/tasks")
    task_event.add_argument("--type", required=True)
    task_event.add_argument("--payload", default="{}", help="JSON object or @FILE")
    task_event.add_argument("--iteration", type=int)
    task_event.add_argument("--case-id")
    task_event.add_argument("--run-id")
    task_event.set_defaults(handler=_task_event)
    task_iterate = task_commands.add_parser("iterate", help="Plan the next optimization iteration and emit its event")
    task_iterate.add_argument("task_id")
    task_iterate.add_argument("--root", default=".aceval/tasks")
    task_iterate.add_argument("--hypothesis", required=True)
    task_iterate.add_argument("--change", action="append", required=True)
    task_iterate.add_argument("--dimension", action="append", default=[])
    task_iterate.set_defaults(handler=_task_iterate)
    task_decide = task_commands.add_parser("decide", help="Record promotion/rejection/convergence for an iteration")
    task_decide.add_argument("task_id")
    task_decide.add_argument("--root", default=".aceval/tasks")
    task_decide.add_argument("--iteration", type=int, required=True)
    task_decide.add_argument("--decision", choices=("promote", "reject", "continue", "converged", "blocked"), required=True)
    task_decide.add_argument("--reason", required=True)
    task_decide.add_argument("--metrics", default="{}")
    task_decide.set_defaults(handler=_task_decide)

    path = commands.add_parser("path", help="Evaluate Skill execution-path conformance")
    path_commands = path.add_subparsers(dest="path_command", required=True)
    path_grade = path_commands.add_parser("grade", help="Grade a complete JSON trace against a path spec")
    path_grade.add_argument("--spec", required=True)
    path_grade.add_argument("--trace", required=True)
    path_grade.add_argument("--trace-incomplete", action="store_true")
    path_grade.add_argument("--output")
    path_grade.set_defaults(handler=_path_grade)
    path_divergence = path_commands.add_parser("divergence", help="Analyze divergence across independent JSON traces")
    path_divergence.add_argument("--case-id", required=True)
    path_divergence.add_argument("--attempts", required=True, help="JSON array or @FILE with attempt_id, trace, outcome, and optional usage")
    path_divergence.add_argument("--spec", help="Optional execution-path spec JSON file")
    path_divergence.add_argument("--output")
    path_divergence.set_defaults(handler=_path_divergence)

    environment = commands.add_parser(
        "environment", help="Build, inspect, and run local isolated validators"
    )
    environment_commands = environment.add_subparsers(
        dest="environment_command", required=True
    )
    environment_build = environment_commands.add_parser(
        "build", help="Build repository.verify/v1 and write a pinned blueprint"
    )
    environment_build.add_argument("--output", required=True)
    environment_build.add_argument("--base-commit", required=True)
    environment_build.add_argument("--head-commit", required=True)
    environment_build.add_argument("--context")
    environment_build.add_argument("--tag", default="aceval/repository-verify:local")
    environment_build.add_argument("--force", action="store_true")
    environment_build.set_defaults(handler=_environment_build)
    environment_check = environment_commands.add_parser(
        "check", help="Check Docker and the exact image required by a blueprint"
    )
    environment_check.add_argument("--blueprint", required=True)
    environment_check.set_defaults(handler=_environment_check)
    environment_bundle = environment_commands.add_parser(
        "bundle", help="Bind an immutable Skill candidate to a local repository tree"
    )
    environment_bundle.add_argument("--repository", required=True)
    environment_bundle.add_argument("--subject-hash", required=True)
    environment_bundle.add_argument("--producer-run-id", required=True)
    environment_bundle.add_argument("--parent-subject-hash")
    environment_bundle.add_argument("--base-commit")
    environment_bundle.add_argument("--head-commit")
    environment_bundle.add_argument("--output", required=True)
    environment_bundle.add_argument("--force", action="store_true")
    environment_bundle.set_defaults(handler=_environment_bundle)
    environment_validate = environment_commands.add_parser(
        "validate", help="Validate a candidate in one ephemeral offline container"
    )
    environment_validate.add_argument("--blueprint", required=True)
    environment_validate.add_argument("--candidate", required=True)
    environment_validate.add_argument("--suite-hash", required=True)
    environment_validate.add_argument("--scenario", required=True)
    environment_validate.add_argument("--run-id", required=True)
    environment_validate.add_argument(
        "--grader-contract", default="code_review_findings_v1@1.0.0"
    )
    environment_validate.add_argument("--output", required=True)
    environment_validate.add_argument("--force", action="store_true")
    environment_validate.set_defaults(handler=_environment_validate)

    profile = commands.add_parser("profile", help="Manage company Agent API profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_validate = profile_commands.add_parser(
        "validate", help="Validate a profile without reading its secret environment value"
    )
    profile_validate.add_argument("path")
    profile_validate.set_defaults(handler=_profile_validate)

    session = commands.add_parser("session", help="Run, fetch, or import online Agent sessions")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_fetch = session_commands.add_parser(
        "fetch", help="Fetch and normalize one online Agent session log"
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
        "execute", help="Create an online Agent session and submit its request"
    )
    session_execute.add_argument("--profile", required=True)
    session_execute.add_argument("--request", required=True, help="JSON file")
    session_execute.set_defaults(handler=_session_execute)
    session_status = session_commands.add_parser(
        "status", help="Query and map one CATX online session status"
    )
    session_status.add_argument("--profile", required=True)
    session_status.add_argument("--session-id", required=True)
    session_status.set_defaults(handler=_session_status)
    session_listen = session_commands.add_parser(
        "listen",
        help="Wait on CATX SSE, then fetch and normalize the complete session log",
    )
    session_listen.add_argument("--profile", required=True)
    session_listen.add_argument("--session-id", required=True)
    session_listen.add_argument("--output", required=True)
    session_listen.add_argument("--force", action="store_true")
    session_listen.set_defaults(handler=_session_listen)
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
    payload = _json_value(value, label)
    if not isinstance(payload, Mapping):
        raise ValueError("%s must be a JSON object" % label)
    return payload


def _json_value(value: str, label: str) -> Any:
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
    return payload


def _text_value(value: Optional[str], label: str) -> Optional[str]:
    if value is None:
        return None
    source = value
    if value.startswith("@"):
        try:
            source = Path(value[1:]).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError("cannot read %s file: %s" % (label, exc)) from exc
    source = source.strip()
    if not source:
        raise ValueError("%s must be non-empty" % label)
    return source


def _pack_state_payload(result: Any) -> Mapping[str, Any]:
    payload = {
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
    experiment_path = getattr(result, "experiment_path", None)
    if experiment_path is not None:
        payload["experiment"] = str(experiment_path)
    return payload


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
        "Review the generated cases, Oracles, and Graders; adjust the separate "
        "ExperimentPlan if needed; then run "
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
    try:
        experiment = resolve_experiment_plan(pack)
        experiment_payload = {
            "configured": True,
            "source": experiment.source,
            "hash": experiment.content_hash,
        }
    except ExperimentPlanError:
        experiment_payload = {"configured": False}
    payload = {
        "ok": report.ok and not unresolved,
        "pack": pack.manifest.metadata.name,
        "version": pack.manifest.metadata.version,
        "pack_hash": pack.pack_hash,
        "suite_hash": pack.suite_hash,
        "calibration_status": lifecycle,
        "optimization_eligible": (
            lifecycle in ("frozen", "legacy")
            and experiment_payload["configured"]
        ),
        "experiment_plan": experiment_payload,
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
    experiment = resolve_experiment_plan(pack, args.experiment)
    result = asyncio.run(
        orchestrator.optimize(
            pack,
            args.subject,
            optimizer,
            max_rounds=args.max_rounds,
            experiment_plan=experiment,
        )
    )
    output_dir = result.output_dir or Path(args.output_root)
    report_paths = write_report(result, output_dir)
    _print_summary(result, report_paths)
    return 0 if result.accepted else 2


def _doctor(args: argparse.Namespace) -> int:
    pack_path = args.pack
    compiled_experiment = None
    if pack_path is None:
        standards = _text_value(args.standards, "standards")
        goal = args.goal or standards
        if not goal:
            raise ValueError(
                "doctor requires --goal or --standards when --pack is omitted"
            )
        output = args.pack_output or str(
            default_output_path(
                args.subject,
                goal,
                standards=standards,
            )
        )
        objective = (
            _json_object(args.objective, "objective") if args.objective else None
        )
        planned = None
        if args.auto_plan:
            if not args.cases:
                raise ValueError(
                    "--auto-plan requires seed --cases; omit --auto-plan to use "
                    "case-free evaluation compilation"
                )
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
        expected_output = (
            _json_value(args.expected_output, "expected_output")
            if args.expected_output is not None
            else None
        )
        reuse_roots = [
            Path.cwd() / "evalpacks",
            Path.cwd() / ".aceval" / "packs",
            Path(output),
        ] + [Path(item) for item in args.suite_root]
        compilation = compile_evaluation(
            args.subject,
            goal,
            output,
            standards=standards,
            cases=cases,
            prompt=args.prompt,
            expected_output=expected_output,
            has_expected_output=args.expected_output is not None,
            pack_type=args.pack_type,
            objective=objective,
            source_root=source_root,
            test_design=test_design,
            reuse_roots=reuse_roots,
            reuse=not args.no_reuse,
            allow_output_variant=args.pack_output is None,
        )
        pack_path = str(compilation.pack.root)
        compiled_experiment = compilation.experiment_path
        evaluation_summary = compilation.summary()
        if compilation.status in ("needs_user_input", "runtime_adapter_required"):
            payload = {
                "ok": True,
                "status": compilation.status,
                "evaluation": evaluation_summary,
                "next_step": (
                    "Provide the requested expected result or a Cases file."
                    if compilation.status == "needs_user_input"
                    else (
                        "Configure a Runtime adapter with: %s."
                        % ", ".join(compilation.runtime_gaps)
                    )
                ),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if planned is not None:
            quality = evaluate_pack_quality(compilation.pack.root)
            if not quality.ready_for_freeze:
                calibrating = begin_calibration(compilation.pack.root)
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
                        "evaluation": evaluation_summary,
                    }
                )
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
                return 0
        labels = compilation.pack.manifest.metadata.labels
        requested_type = labels.get("aceval.requested_type", "generic")
        if requested_type not in PACK_TYPES:
            calibrating = begin_calibration(compilation.pack.root)
            payload = dict(_pack_state_payload(calibrating))
            payload.update(
                {
                    "status": "calibration_required",
                    "reason": (
                        "The requested type used the generic fallback. Review and "
                        "calibrate its evaluator before a separate explicit freeze."
                    ),
                    "evaluation": evaluation_summary,
                }
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        lifecycle = pack_calibration_status(compilation.pack)
        if lifecycle not in ("frozen", "legacy") and not args.approve_pack:
            calibrating = begin_calibration(compilation.pack.root)
            payload = dict(_pack_state_payload(calibrating))
            payload.update(
                {
                    "status": "calibration_required",
                    "reason": (
                        "Generated Pack is not allowed to optimize the Skill until "
                        "you explicitly approve its evaluation contract."
                    ),
                    "evaluation": evaluation_summary,
                }
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if lifecycle not in ("frozen", "legacy"):
            freeze_evalpack(compilation.pack.root, approve=True)
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
    experiment = resolve_experiment_plan(
        pack,
        args.experiment or compiled_experiment,
    )
    result = asyncio.run(
        orchestrator.optimize(
            pack,
            args.subject,
            optimizer,
            max_rounds=args.max_rounds,
            experiment_plan=experiment,
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
    profile = _load_online_profile(args.path)
    if isinstance(profile, CatxAgentProfile):
        payload = {
            "ok": True,
            "api_version": profile.api_version,
            "name": profile.name,
            "base_url": profile.base_url,
            "credential_envs": {
                "api_key": profile.api_key_env,
                "user_mis_id": profile.user_mis_id_env,
                "agent_id": profile.agent_id_env,
                "environment_id": profile.environment_id_env,
                "repository_authorization_token": (
                    profile.repository_resources[0].authorization_token_env
                    if profile.repository_resources
                    else None
                ),
                "repository_authorization_tokens": sorted(
                    {
                        item.authorization_token_env
                        for item in profile.repository_resources
                    }
                ),
                "stream_api_key": (
                    profile.stream.api_key_env if profile.stream is not None else None
                ),
                "stream_bearer": (
                    profile.stream.bearer_env if profile.stream is not None else None
                ),
            },
            "events_limit": profile.events_limit,
            "credentials_file_configured": profile.credentials_file is not None,
            "stream_configured": profile.stream is not None,
            "repository": (
                {
                    "configured": True,
                    "url": profile.repository_resources[0].url,
                    "mount_path": profile.repository_resources[0].mount_path,
                    "count": len(profile.repository_resources),
                    "resources": [
                        {"url": item.url, "mount_path": item.mount_path}
                        for item in profile.repository_resources
                    ],
                }
                if profile.repository_resources
                else {"configured": False}
            ),
            "secret_loaded": False,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
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


def _code_review_init_lab(args: argparse.Namespace) -> int:
    stacks = tuple(args.stack or DEFAULT_CODE_REVIEW_STACKS)
    cases = create_code_review_fixture_lab(
        args.output,
        force=bool(args.force),
        stacks=stacks,
    )
    root = Path(args.output).expanduser().absolute()
    print(
        json.dumps(
            {
                "ok": True,
                "api_version": "aceval.code-review-lab/v1",
                "output": str(root),
                "lab": str(root / "lab.json"),
                "evals": str(root / "evals" / "evals.json"),
                "stacks": list(stacks),
                "default_case": (
                    DEFAULT_CODE_REVIEW_CASE
                    if any(case.id == DEFAULT_CODE_REVIEW_CASE for case in cases)
                    else cases[0].id
                ),
                "cases": [
                    {
                        "id": case.id,
                        "stack": case.stack,
                        "language": case.language,
                        "case_type": case.case_type,
                        "repository": str(case.repository),
                        "base_ref": case.base_ref,
                        "head_ref": case.head_ref,
                        "base_commit": case.base_commit,
                        "head_commit": case.head_commit,
                        "oracle": str(case.oracle),
                    }
                    for case in cases
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _code_review_select_cases(args: argparse.Namespace) -> int:
    lab_root = Path(args.lab).expanduser().resolve()
    lab = _json_object("@" + str(lab_root / "lab.json"), "Fixture Lab")
    if args.stack:
        stacks = tuple(args.stack)
        source = "user_override"
    else:
        if not args.skill:
            raise ValueError("select-cases requires --skill or at least one --stack")
        skill_path = Path(args.skill).expanduser().resolve()
        if skill_path.is_dir():
            skill_path = skill_path / "SKILL.md"
        try:
            if skill_path.stat().st_size > 1024 * 1024:
                raise ValueError("Skill instructions exceed 1 MiB")
            content = skill_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError("cannot read Skill instructions: %s" % exc) from exc
        stacks = infer_code_review_stacks(content)
        source = "skill_content"
    raw_cases = lab.get("cases")
    if not isinstance(raw_cases, list) or any(not isinstance(item, Mapping) for item in raw_cases):
        raise ValueError("Fixture Lab cases must be an array of objects")
    selected = [dict(item) for item in raw_cases if item.get("stack") in stacks]
    if not selected:
        raise ValueError("Fixture Lab does not contain the selected stacks")
    repository = lab.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Fixture Lab is missing repository")
    print(
        json.dumps(
            {
                "ok": True,
                "selection_source": source,
                "stacks": list(stacks),
                "repository": str((lab_root / repository).resolve()),
                "cases": selected,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _code_review_grade(args: argparse.Namespace) -> int:
    findings = _json_value(args.findings, "findings")
    oracle_data = _json_object(args.oracle, "oracle")
    observation = repository_observation(
        args.repository,
        findings,
        revision=oracle_data.get("head_commit"),
    )
    result = asyncio.run(
        CodeReviewFindingsGrader().evaluate(observation, Oracle(data=oracle_data), {})
    )
    payload = as_primitive(result)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.status == GradeStatus.PASS else 1


def _code_review_online_run(args: argparse.Namespace) -> int:
    profile = _load_online_profile(args.profile)
    if not isinstance(profile, CatxAgentProfile):
        raise ValueError("code-review online-run requires a CATX profile")
    stacks = tuple(
        args.stack
        or ("typescript-web", "react-native", "wechat-miniprogram")
    )
    execution_path = None
    if args.execution_path:
        execution_path = ExecutionPathSpec.from_mapping(
            _json_value("@" + args.execution_path, "execution path")
        )
    store = TaskStore(args.task_root) if args.task_id else None
    summary = run_online_review_suite(
        profile,
        lab_root=args.lab,
        skill_ref=args.skill_ref,
        skill_commit=args.skill_commit,
        workspace=args.workspace,
        configuration=args.configuration,
        stacks=stacks,
        case_ids=tuple(args.case or ()),
        task_store=store,
        task_id=args.task_id,
        iteration=args.iteration,
        execution_path=execution_path,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _code_review_online_report(args: argparse.Namespace) -> int:
    benchmark = write_online_review_benchmark(
        args.workspace,
        skill_name=args.skill_name,
        skill_path=args.skill_path,
    )
    print(json.dumps(benchmark["run_summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _console_build(args: argparse.Namespace) -> int:
    result = build_console(
        args.workspace,
        args.output,
        plan_path=args.plan,
        profile_path=args.profile,
        task_root=args.task_root,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _console_serve(args: argparse.Namespace) -> int:
    print(
        "Skill Optimization Cockpit: http://%s:%d" % (args.host, args.port),
        file=sys.stderr,
        flush=True,
    )
    serve_console(
        args.workspace,
        plan_path=args.plan,
        profile_path=args.profile,
        task_root=args.task_root,
        host=args.host,
        port=args.port,
    )
    return 0


def _d2c_check(args: argparse.Namespace) -> int:
    report = check_d2c_profile(D2CBrowserProfile.load(args.profile))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


def _d2c_validate(args: argparse.Namespace) -> int:
    request = D2CValidationRequest.load(args.request)
    store = TaskStore(args.task_root) if args.task_id else None
    if store and args.task_id:
        current = int(store.load(args.task_id).get("current_iteration", 0))
        store.update(args.task_id, status="running", current_iteration=max(current, args.iteration))
        store.append_event(args.task_id, "case_run.started", {"provider": "local-browser", "url": request.url}, iteration=args.iteration, case_id=request.case_id, run_id="browser-1")
    try:
        receipt = validate_d2c(D2CBrowserProfile.load(args.profile), request, args.output)
    except Exception as exc:
        if store and args.task_id:
            store.append_event(args.task_id, "case_run.failed", {"provider": "local-browser", "error": str(exc)}, iteration=args.iteration, case_id=request.case_id, run_id="browser-1")
            store.update(args.task_id, status="blocked")
        raise
    if store and args.task_id:
        store.append_event(
            args.task_id,
            "case_run.completed",
            {"provider": "local-browser", "status": receipt["status"], "receipt": str(Path(args.output).expanduser().resolve() / "receipt.json"), "artifacts": receipt["artifacts"]},
            iteration=args.iteration,
            case_id=request.case_id,
            run_id="browser-1",
        )
        store.update(args.task_id, status="ready")
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["status"] == ValidationStatus.SUCCEEDED.value else 1


def _kernel(args: argparse.Namespace) -> IterationKernel:
    return IterationKernel(args.task_root)


def _kernel_create(args: argparse.Namespace) -> int:
    result = _kernel(args).create_task(KernelConfig.load(args.config), KernelInput.load(args.input), task_id=args.id)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_status(args: argparse.Namespace) -> int:
    value = _kernel(args)
    print(json.dumps(value.snapshot(args.task_id), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_log(args: argparse.Namespace) -> int:
    value = _kernel(args).session_log(args.task_id, args.iteration, args.purpose, args.case)
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_compile(args: argparse.Namespace) -> int:
    print(json.dumps(_kernel(args).compile_design(args.task_id), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_dispatch(args: argparse.Namespace) -> int:
    print(json.dumps(_kernel(args).dispatch(args.task_id, purpose=args.purpose, case_ids=tuple(args.case)), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_collect(args: argparse.Namespace) -> int:
    print(json.dumps(_kernel(args).collect(args.task_id, wait=bool(args.wait)), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_analyze(args: argparse.Namespace) -> int:
    print(json.dumps(_kernel(args).analyze(args.task_id), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_advance(args: argparse.Namespace) -> int:
    print(json.dumps(_kernel(args).advance(args.task_id, wait=bool(args.wait)), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_run(args: argparse.Namespace) -> int:
    print(json.dumps(_kernel(args).run_until_gate(args.task_id, max_steps=args.max_steps), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_confirm(args: argparse.Namespace) -> int:
    if args.supplement:
        supplement = KernelInput.load(args.supplement)
        approve = False
    else:
        supplement = None
        approve = bool(args.approve) and not bool(args.reject)
    print(json.dumps(_kernel(args).confirm(
        args.task_id,
        approve=approve,
        supplement=supplement,
        selected_capability_ids=tuple(args.select_capability),
        selected_change_ids=tuple(args.select_change),
        user_feedback=args.feedback,
    ), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_optimize(args: argparse.Namespace) -> int:
    print(json.dumps(_kernel(args).optimize(args.task_id), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _kernel_publish(args: argparse.Namespace) -> int:
    print(json.dumps(_kernel(args).publish_candidate(args.task_id), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _optional_json(value: Optional[str], label: str, default: Any) -> Any:
    if not value:
        return default
    return _json_value(value, label)


def _task_create(args: argparse.Namespace) -> int:
    cases = _optional_json(args.cases, "cases", [])
    if not isinstance(cases, list):
        raise ValueError("cases must be a JSON array")
    evaluation = _optional_json(args.evaluation_spec, "evaluation_spec", None)
    if evaluation is not None and not isinstance(evaluation, Mapping):
        raise ValueError("evaluation_spec must be a JSON object")
    environment = _optional_json(args.environment, "environment", {})
    if not isinstance(environment, Mapping):
        raise ValueError("environment must be a JSON object")
    task = TaskStore(args.root).create(
        task_id=args.id,
        skill_name=args.skill_name,
        skill_source=args.skill_source,
        scenario=args.scenario,
        goal=args.goal,
        standards=tuple(args.standard),
        cases=tuple(cases),
        evaluation_spec=evaluation,
        environment=environment,
    )
    print(json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _task_list(args: argparse.Namespace) -> int:
    print(json.dumps({"tasks": TaskStore(args.root).list()}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _task_show(args: argparse.Namespace) -> int:
    store = TaskStore(args.root)
    print(json.dumps({"task": store.load(args.task_id), "events": store.events(args.task_id)}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _task_event(args: argparse.Namespace) -> int:
    payload = _json_value(args.payload, "event payload")
    if not isinstance(payload, Mapping):
        raise ValueError("event payload must be a JSON object")
    event = TaskStore(args.root).append_event(
        args.task_id, args.type, payload,
        iteration=args.iteration, case_id=args.case_id, run_id=args.run_id,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _task_iterate(args: argparse.Namespace) -> int:
    event = TaskStore(args.root).begin_iteration(
        args.task_id,
        hypothesis=args.hypothesis,
        planned_changes=tuple(args.change),
        target_dimensions=tuple(args.dimension),
    )
    print(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _task_decide(args: argparse.Namespace) -> int:
    metrics = _json_value(args.metrics, "metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("metrics must be a JSON object")
    event = TaskStore(args.root).record_decision(
        args.task_id,
        iteration=args.iteration,
        decision=args.decision,
        reason=args.reason,
        metrics=metrics,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _path_grade(args: argparse.Namespace) -> int:
    spec = ExecutionPathSpec.from_mapping(_json_value("@" + args.spec, "path spec"))
    trace = _json_value("@" + args.trace, "trace")
    if not isinstance(trace, list) or not all(isinstance(item, Mapping) for item in trace):
        raise ValueError("trace must be a JSON array of objects")
    result = evaluate_trace_conformance(spec, trace, trace_complete=not args.trace_incomplete)
    if args.output:
        _write_json_output(args.output, result, False)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


def _path_divergence(args: argparse.Namespace) -> int:
    attempts = _json_value(args.attempts, "attempts")
    if not isinstance(attempts, list) or not all(isinstance(item, Mapping) for item in attempts):
        raise ValueError("attempts must be a JSON array of objects")
    spec = None
    if args.spec:
        spec = ExecutionPathSpec.from_mapping(_json_value("@" + args.spec, "path spec"))
    result = analyze_divergence(args.case_id, attempts, path_spec=spec).to_dict()
    if args.output:
        _write_json_output(args.output, result, False)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _repository_verify_context(value: Optional[str]) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "environments" / "repository-verify"


def _environment_build(args: argparse.Namespace) -> int:
    context = _repository_verify_context(args.context)
    image_id = build_repository_verify_image(context, tag=args.tag)
    blueprint = repository_verify_blueprint(
        image_id,
        base_commit=args.base_commit,
        head_commit=args.head_commit,
    )
    output = write_contract(args.output, blueprint, force=bool(args.force))
    print(
        json.dumps(
            {
                "ok": True,
                "provider": blueprint.provider,
                "image_id": image_id,
                "blueprint_hash": blueprint.blueprint_hash,
                "output": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _environment_check(args: argparse.Namespace) -> int:
    blueprint = EnvironmentBlueprint.load(args.blueprint)
    report = LocalDockerProvider().preflight(blueprint)
    payload = as_primitive(report)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.ready else 1


def _environment_bundle(args: argparse.Namespace) -> int:
    bundle = create_candidate_bundle(
        args.repository,
        subject_hash=args.subject_hash,
        producer_run_id=args.producer_run_id,
        parent_subject_hash=args.parent_subject_hash,
        base_commit=args.base_commit,
        result_commit=args.head_commit,
        metadata={"scenario": "code-review"},
    )
    output = write_contract(args.output, bundle, force=bool(args.force))
    print(
        json.dumps(
            {
                "ok": True,
                "api_version": CANDIDATE_BUNDLE_API_VERSION,
                "bundle_hash": bundle.bundle_hash,
                "artifact_sha256": bundle.artifact_sha256,
                "output": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _environment_validate(args: argparse.Namespace) -> int:
    request = ValidationRequest(
        api_version=VALIDATION_REQUEST_API_VERSION,
        run_id=args.run_id,
        suite_hash=args.suite_hash,
        scenario_id=args.scenario,
        candidate=CandidateBundle.load(args.candidate),
        blueprint=EnvironmentBlueprint.load(args.blueprint),
        grader_contract=args.grader_contract,
    )
    receipt = LocalDockerProvider().validate_once(request)
    output = write_contract(args.output, receipt, force=bool(args.force))
    payload = {
        "ok": receipt.status == ValidationStatus.SUCCEEDED,
        "status": receipt.status.value,
        "request_hash": request.request_hash,
        "receipt_hash": receipt.receipt_hash,
        "output": str(output),
        "infrastructure_errors": tuple(receipt.infrastructure_errors),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt.status == ValidationStatus.SUCCEEDED else 1


def _load_online_profile(path_value: str) -> Any:
    """Dispatch profiles by version without ever reading configured secrets."""

    path = Path(path_value)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read online Agent profile as JSON") from exc
    if isinstance(document, Mapping) and document.get("api_version") == CATX_PROFILE_API_VERSION:
        return CatxAgentProfile.load(path)
    return CompanyApiProfile.load(path)


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
    profile = _load_online_profile(args.profile)
    if isinstance(profile, CatxAgentProfile):
        bundle = CatxAgentClient(profile).fetch_session(args.session_id)
    else:
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
    profile = _load_online_profile(args.profile)
    request = _json_object("@" + args.request, "request")
    if isinstance(profile, CatxAgentProfile):
        session_id = CatxAgentClient(profile).start_session(request)
    else:
        session_id = HTTPSessionLogProvider(profile).execute(request)
    print(
        json.dumps(
            {"ok": True, "profile": profile.name, "session_id": session_id},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _session_status(args: argparse.Namespace) -> int:
    profile = _load_online_profile(args.profile)
    if not isinstance(profile, CatxAgentProfile):
        raise ValueError("session status currently requires a CATX profile")
    result = CatxAgentClient(profile).poll_session(args.session_id)
    payload = {"ok": True, "profile": profile.name, "session_id": args.session_id}
    payload.update(result)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _session_listen(args: argparse.Namespace) -> int:
    profile = _load_online_profile(args.profile)
    if not isinstance(profile, CatxAgentProfile):
        raise ValueError("session listen currently requires a CATX profile")
    bundle = CatxAgentClient(profile).listen_session(args.session_id)
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
