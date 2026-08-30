import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from aceval.agent_runtime import ModelReply
from aceval.catx_bindings import CatxBindingEvidence
from aceval.iteration_kernel import IterationKernel, IterationKernelError, _attach_code_review_fixtures, _merge_model_path, _path_for_case
from aceval.kernel_contracts import KERNEL_CONFIG_API_VERSION, KERNEL_INPUT_API_VERSION, KernelConfig, KernelInput


class PassingGateway:
    def __init__(self):
        self.counter = 0

    def start_session(self, request):
        self.counter += 1
        return "session-%d" % self.counter

    def poll_session(self, session_id):
        return {"status": "COMPLETED", "session_id": session_id}

    def fetch_session(self, session_id):
        return {
            "schema_version": "aceval.imported-session/v1",
            "session_id": session_id,
            "completeness": {"trace": True, "output": True},
            "observation": {
                "output": "ok",
                "trace": [{"kind": "tool_call", "name": "read_file", "path": "SKILL.md"}],
                "metadata": {},
                "usage": {"total_tokens": 12},
                "error": None,
            },
        }


class ImprovingGateway(PassingGateway):
    """Fail the Champion once, then return the expected result for Challenger runs."""

    def __init__(self):
        super().__init__()
        self.bindings = []

    def start_session(self, request, *, binding=None):
        self.counter += 1
        self.bindings.append(binding)
        return "session-%d" % self.counter

    def verify_binding(self, session_id, binding):
        return CatxBindingEvidence(
            verified=True,
            requested_binding_hash=binding.binding_hash,
            actual_subject_hash=binding.subject_hash,
            actual_repository_hash=binding.repository_hash,
            actual_base_commit=binding.base_commit,
            actual_head_commit=binding.head_commit,
        )

    def fetch_session(self, session_id):
        first_round = session_id == "session-1"
        return {
            "schema_version": "aceval.imported-session/v1",
            "session_id": session_id,
            "completeness": {"trace": True, "output": True},
            "observation": {
                "output": "not grounded" if first_round else "grounded review",
                "trace": [{"kind": "tool_call", "name": "read_file", "path": "SKILL.md"}],
                "metadata": {},
                "usage": {"total_tokens": 12},
                "error": None,
            },
        }


class PassingAnalysisModel:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        request = json.loads(messages[-1]["content"])
        has_verification = bool(request.get("verification_evidence"))
        return ModelReply(
            content=json.dumps(
                {
                    "case_results": [
                        {
                            "case_id": case["id"],
                            "status": "pass",
                            "verification_status": "pass" if has_verification else "not_run",
                            "reason": "evidence satisfies the standard",
                            "evidence_refs": [],
                        }
                        for case in request["cases"]
                    ],
                    "failure_clusters": [],
                    "conflicts": [],
                    "proposed_changes": [],
                    "target_scope": [],
                }
            ),
            usage={"input_tokens": 80, "output_tokens": 20},
        )


class RepairModel:
    def complete(self, messages, tools):
        request = json.loads(messages[-1]["content"])
        if "current_skill" in request:
            revised = request["current_skill"] + "\n## Evidence gate\n\nBefore reporting a finding, cite the source location and explain the impact.\n"
            return ModelReply(content=json.dumps({"skill_markdown": revised, "rationale": "Require grounded evidence"}), usage={"total_tokens": 30})
        return ModelReply(
            content=json.dumps(
                {
                    "case_results": [
                        {"case_id": case["id"], "status": "fail", "verification_status": "not_run", "reason": "result is not grounded", "evidence_refs": []}
                        for case in request["cases"]
                    ],
                    "failure_clusters": [{"id": "grounding", "case_ids": [case["id"] for case in request["cases"]], "root_cause": "missing evidence gate", "skill_change_authorized": True}],
                    "conflicts": [],
                    "proposed_changes": [{"target": "SKILL.md", "change": "add an evidence gate", "why": "prevent unsupported findings", "case_ids": [case["id"] for case in request["cases"]]}],
                    "target_scope": ["SKILL.md"],
                }
            ),
            usage={"total_tokens": 40},
        )


class MultiFileRepairModel:
    def complete(self, messages, tools):
        request = json.loads(messages[-1]["content"])
        if "current_files" in request:
            return ModelReply(content=json.dumps({
                "changes": [
                    {"path": "scripts/check.py", "operation": "replace_text", "old_text": "return False", "new_text": "return bool(evidence)", "reason": "use collected evidence"},
                    {"path": "references/rules.md", "operation": "replace_text", "old_text": "Missing evidence means pass.", "new_text": "Missing evidence means not evaluable.", "reason": "do not infer success"},
                ],
                "rationale": "Align executable and reference behavior.",
            }))
        return ModelReply(content=json.dumps({
            "case_results": [{"case_id": case["id"], "status": "fail", "verification_status": "not_run", "reason": "execution and rules disagree", "evidence_refs": []} for case in request["cases"]],
            "failure_clusters": [{"id": "resource-drift", "case_ids": [case["id"] for case in request["cases"]], "root_cause": "script and reference encode incorrect behavior", "skill_change_authorized": True}],
            "conflicts": [],
            "proposed_changes": [{"target": "scripts/check.py", "change": "use evidence", "why": "correct execution", "case_ids": [case["id"] for case in request["cases"]]}],
            "target_scope": ["scripts/check.py", "references/rules.md"],
        }))


class FakePublisher:
    def __init__(self):
        self.calls = []

    def publish(self, repository, candidate_root, *, message):
        self.calls.append((repository, candidate_root, message))
        return "a" * 40


class ApplyingFakePublisher(FakePublisher):
    """Apply the approved candidate bytes without requiring a network push."""

    def publish(self, repository, candidate_root, *, message):
        self.calls.append((repository, candidate_root, message))
        manifest = json.loads(
            (Path(candidate_root) / "candidate.manifest.json").read_text(encoding="utf-8")
        )
        skill_root = Path(repository.local_path)
        for relative in manifest["changed_paths"]:
            target = skill_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(candidate_root) / relative, target)
        return "a" * 40


class FakeCheckoutManager:
    def __init__(self, skill_path):
        self.skill_path = skill_path
        self.calls = []

    def prepare(self, repository, destination):
        self.calls.append((repository, destination))
        return replace(repository, local_path=str(self.skill_path))


class IterationKernelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text(
            """# Review Skill

## Capabilities

### Review source

Inputs:
- repository

Outputs:
- review result

Steps:
1. Use `read_file` to read the changed source.
2. Return only evidence-backed findings.
""",
            encoding="utf-8",
        )
        self.config = KernelConfig.from_mapping(
            {
                "api_version": KERNEL_CONFIG_API_VERSION,
                "skill_repository": {
                    "ssh_url": "ssh://git@git.example/reviewer.git",
                    "branch": "feature/eval",
                    "local_path": str(self.skill),
                    "authorization_token_env": "REPO_PAT",
                    "mount_path": "/workspace/skill",
                },
                "code_repository": None,
                "local_analysis": {"model_command": ["unused"], "model_id": "fake"},
                "remote_agent": {"profile_path": str(self.root / "unused-catx.json"), "poll_interval_seconds": 1},
                "policy": {"max_generated_cases": 1, "max_rounds": 3},
            }
        )
        self.user_input = KernelInput.from_mapping(
            {
                "api_version": KERNEL_INPUT_API_VERSION,
                "skill_name": "reviewer",
                "goal": "Produce an evidence-backed review",
                "standards": ["Return the expected result", "Read the Skill before acting"],
                "cases": [{"id": "review-1", "prompt": "Review the fixture", "expected_output": "ok"}],
            }
        )
        self.gateway = PassingGateway()
        self.model = PassingAnalysisModel()
        self.kernel = IterationKernel(
            self.root / "tasks",
            gateway_factory=lambda config: self.gateway,
            model_factory=lambda config: self.model,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_code_review_fixtures_are_bound_per_case_and_missing_cases_stay_blocked(self):
        lab = self.root / "fixture-lab"
        repository = lab / "repository"
        repository.mkdir(parents=True)
        (lab / "lab.json").write_text(
            json.dumps({
                "cases": [{
                    "id": "bound",
                    "base_ref": "base/main",
                    "head_ref": "case/bound",
                    "base_commit": "1" * 40,
                    "head_commit": "2" * 40,
                    "oracle": "cases/bound/oracle.json",
                }]
            }),
            encoding="utf-8",
        )
        cases, summary = _attach_code_review_fixtures(
            ({"id": "bound", "prompt": "Review"}, {"id": "missing", "prompt": "Review another"}),
            code_root=str(repository),
        )
        self.assertEqual("ready", cases[0]["metadata"]["fixture_status"])
        self.assertEqual("case/bound", cases[0]["metadata"]["fixture_branch"])
        self.assertEqual("missing_pr_binding", cases[1]["metadata"]["fixture_status"])
        self.assertEqual(["bound"], summary["ready_case_ids"])
        self.assertEqual(["missing"], summary["missing_case_ids"])

    def test_model_path_cannot_remove_locked_skill_loading_or_publish_guards(self):
        case = {
            "id": "near-miss",
            "prompt": "Write a story",
            "metadata": {"harness_case_kind": "negative_trigger", "aceval_test": {"title": "触发边界"}},
        }
        model_path = {
            "case_id": "near-miss",
            "purpose": "模型建议路径",
            "steps": [
                {
                    "id": "do-not-load-skill",
                    "label": "允许读取 Skill",
                    "kind": "recommended",
                    "match": {"contains": "SKILL.md"},
                    "after": [],
                },
                {
                    "id": "business-check",
                    "label": "检查用户任务",
                    "kind": "required",
                    "match": {"tool_name": "read_file"},
                    "after": [],
                },
            ],
        }
        merged = _merge_model_path(case, model_path, {})
        by_id = {step["id"]: step for step in merged["steps"]}
        self.assertEqual("forbidden", by_id["do-not-load-skill"]["kind"])
        self.assertEqual("forbidden", by_id["no-skill-publish"]["kind"])
        self.assertIn("business-check", by_id)
        self.assertEqual("recommended", by_id["business-check"]["kind"])
        self.assertEqual("required", by_id["business-check"]["model_requested_kind"])
        self.assertTrue(merged["rejected_model_steps"])
        deterministic = _path_for_case(case, {})
        self.assertEqual(
            {step["id"] for step in deterministic["steps"]},
            set(merged["system_step_ids"]),
        )

    def test_complete_evaluation_and_stability_verification_converges(self):
        created = self.kernel.create_task(self.config, self.user_input, task_id="kernel-task-001")
        self.assertEqual("created", created["state"]["phase"])
        design = self.kernel.compile_design("kernel-task-001")
        self.assertIn("review-1", design["case_ids"])
        self.assertGreaterEqual(len(design["case_ids"]), 1)
        self.kernel.dispatch("kernel-task-001")
        self.kernel.collect("kernel-task-001")
        first = self.kernel.analyze("kernel-task-001")
        self.assertEqual("verify_passes", first["next_action"])
        self.kernel.dispatch("kernel-task-001", purpose="pass-verification", case_ids=first["verification_required_case_ids"])
        self.kernel.collect("kernel-task-001")
        final = self.kernel.analyze("kernel-task-001")
        self.assertEqual("converged", final["next_action"])
        self.assertEqual("quality_target_met", final["convergence"]["reason"])
        snapshot = self.kernel.snapshot("kernel-task-001")
        self.assertEqual("converged", snapshot["state"]["phase"])
        self.assertEqual("converged", snapshot["task"]["status"])
        self.assertIn("design", snapshot)
        self.assertIn("decision", snapshot)
        session_logs = snapshot["iterations"][0]["session_logs"]
        self.assertEqual(2 * len(snapshot["design"]["case_ids"]), len(session_logs))
        self.assertEqual({"evaluation", "pass-verification"}, {item["purpose"] for item in session_logs})
        self.assertTrue(all(item["attempt_number"] == 1 for item in session_logs))
        self.assertTrue(all(len(item["attempts"]) == 1 for item in session_logs))

    def test_analysis_can_resume_from_every_persisted_brain_stage(self):
        for index, phase in enumerate(("evidence_ready", "semantic_grading", "attribution", "proposal"), start=1):
            with self.subTest(phase=phase):
                task_id = "resume-stage-%d" % index
                self.kernel.create_task(self.config, self.user_input, task_id=task_id)
                self.kernel.compile_design(task_id)
                self.kernel.dispatch(task_id)
                self.kernel.collect(task_id)
                self.kernel._transition(task_id, phase, brain_stage=phase)
                result = self.kernel.advance(task_id)
                self.assertEqual("running", result["status"])
                self.assertEqual("verification_running", self.kernel.state(task_id)["phase"])
                receipts = list(
                    (self.root / "tasks" / task_id / "iterations" / "iteration-000" / "analysis").glob(
                        "agent-call-receipt-*.json"
                    )
                )
                self.assertTrue(receipts)

    def test_pass_verification_fails_closed_when_trial_environment_drifts(self):
        task_id = "environment-drift"
        self.kernel.create_task(self.config, self.user_input, task_id=task_id)
        self.kernel.compile_design(task_id)
        self.kernel.dispatch(task_id)
        self.kernel.collect(task_id)
        decision = self.kernel.analyze(task_id)
        self.assertEqual("verify_passes", decision["next_action"])
        (self.skill / "SKILL.md").write_text(
            (self.skill / "SKILL.md").read_text(encoding="utf-8") + "\nUnexpected drift.\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(IterationKernelError, "environment drift"):
            self.kernel.dispatch(
                task_id,
                purpose="pass-verification",
                case_ids=decision["verification_required_case_ids"],
            )

    def test_pre_session_contract_can_be_upgraded_with_generated_fixture_revisions(self):
        task_id = "fixture-contract-upgrade"
        self.kernel.create_task(self.config, self.user_input, task_id=task_id)
        state = self.kernel.state(task_id)
        original_design = {"cases": [{"id": "review-1", "case_revision": "revision-1"}]}
        frozen = self.kernel._ensure_environment_contract(
            task_id, design=original_design, state=state, iteration=0
        )
        upgraded_design = {
            "cases": [{
                "id": "review-1",
                "case_revision": "revision-1",
                "metadata": {"fixture_revision": "a" * 40},
            }]
        }
        refreshed = self.kernel._ensure_environment_contract(
            task_id,
            design=upgraded_design,
            state=self.kernel.state(task_id),
            iteration=0,
        )
        self.assertNotEqual(frozen["contract_hash"], refreshed["contract_hash"])
        self.assertIn(
            "environment.contract_refreshed",
            [event["type"] for event in self.kernel.store.events(task_id)],
        )

    def test_catx_binding_requires_a_real_commit_and_active_subject_snapshot(self):
        task_id = "binding-material"
        kernel = IterationKernel(
            self.root / "binding-material-tasks",
            gateway_factory=lambda config: ImprovingGateway(),
            model_factory=lambda config: self.model,
        )
        kernel.create_task(self.config, self.user_input, task_id=task_id)
        kernel.compile_design(task_id)
        with self.assertRaisesRegex(IterationKernelError, "repository commit is missing or invalid"):
            kernel.dispatch(task_id)

    def test_generated_cases_have_reusable_identity_and_applicability(self):
        task_id = "case-identity"
        self.kernel.create_task(self.config, self.user_input, task_id=task_id)
        design = self.kernel.compile_design(task_id)
        for case in design["cases"]:
            self.assertTrue(case["case_revision"].startswith("sha256:"))
            self.assertTrue(case["content_hash"].startswith("sha256:"))
            self.assertTrue(case["reuse_key"].startswith("sha256:"))
            self.assertIn("source", case["provenance"])
            self.assertEqual("catx", case["environment_applicability"]["trial_provider"])

    def test_failed_cases_require_confirmation_before_candidate_and_next_round(self):
        publisher = FakePublisher()
        failing_input = KernelInput.from_mapping(
            {
                "api_version": KERNEL_INPUT_API_VERSION,
                "skill_name": "reviewer",
                "goal": "Produce an evidence-backed review",
                "standards": ["Return the expected result"],
                "cases": [
                    {
                        "id": "review-1",
                        "prompt": "Review the fixture",
                        "expected_output": "grounded review",
                    }
                ],
            }
        )
        kernel = IterationKernel(
            self.root / "repair-tasks",
            gateway_factory=lambda config: self.gateway,
            model_factory=lambda config: RepairModel(),
            publisher=publisher,
        )
        kernel.create_task(self.config, failing_input, task_id="repair-task-001")
        kernel.compile_design("repair-task-001")
        kernel.dispatch("repair-task-001")
        kernel.collect("repair-task-001")
        decision = kernel.analyze("repair-task-001")
        if decision["next_action"] == "verify_passes":
            kernel.dispatch("repair-task-001", purpose="pass-verification", case_ids=decision["verification_required_case_ids"])
            kernel.collect("repair-task-001")
            decision = kernel.analyze("repair-task-001")
        self.assertEqual("await_user_confirmation", decision["next_action"])
        decision_path = Path(kernel.state("repair-task-001")["decision"])
        immutable_decision = decision_path.read_bytes()
        with self.assertRaises(Exception):
            kernel.optimize("repair-task-001")
        kernel.confirm("repair-task-001", approve=True)
        self.assertEqual(immutable_decision, decision_path.read_bytes())
        approvals = list((decision_path.parent / "approvals").glob("approval-record-*.json"))
        self.assertEqual(1, len(approvals))
        self.assertTrue(kernel.state("repair-task-001")["approved_decision"])
        candidate = kernel.optimize("repair-task-001")
        self.assertTrue(Path(candidate["path"], "SKILL.md").is_file())
        self.assertEqual("candidate_ready", kernel.state("repair-task-001")["phase"])
        with self.assertRaisesRegex(IterationKernelError, "explicit user approval"):
            kernel.publish_candidate("repair-task-001")
        self.assertEqual([], publisher.calls)
        gate = kernel.run_until_gate("repair-task-001")
        self.assertEqual("candidate_ready", gate["gate"])
        kernel.confirm("repair-task-001", approve=True)
        published = kernel.publish_candidate("repair-task-001")
        self.assertEqual(1, published["iteration"])
        self.assertEqual("evaluation_ready", published["state"]["phase"])
        self.assertIsNone(published["state"]["environment_contract_hash"])
        second_round = kernel.dispatch("repair-task-001")
        self.assertEqual("running", second_round["status"])
        self.assertEqual("remote_running", kernel.state("repair-task-001")["phase"])
        self.assertEqual(1, len(publisher.calls))

    def test_approved_candidate_is_promoted_after_paired_next_round_evaluation(self):
        subprocess.run(
            ("git", "init", "-b", "feature/eval"),
            cwd=self.skill,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(("git", "config", "user.name", "Aceval Test"), cwd=self.skill, check=True)
        subprocess.run(("git", "config", "user.email", "aceval@example.invalid"), cwd=self.skill, check=True)
        subprocess.run(("git", "add", "SKILL.md"), cwd=self.skill, check=True)
        subprocess.run(
            ("git", "commit", "-m", "champion"),
            cwd=self.skill,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        champion = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=self.skill,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode("ascii").strip()
        gateway = ImprovingGateway()
        publisher = ApplyingFakePublisher()
        kernel = IterationKernel(
            self.root / "promotion-tasks",
            gateway_factory=lambda config: gateway,
            model_factory=lambda config: RepairModel(),
            publisher=publisher,
        )
        evaluation = KernelInput.from_mapping(
            {
                "api_version": KERNEL_INPUT_API_VERSION,
                "skill_name": "reviewer",
                "goal": "Produce an evidence-backed review",
                "standards": ["Return the expected result"],
                "cases": [
                    {
                        "id": "review-1",
                        "prompt": "Review the fixture",
                        "expected_output": "grounded review",
                    }
                ],
            }
        )
        task_id = "promotion-cycle"

        created = kernel.create_task(self.config, evaluation, task_id=task_id)
        self.assertEqual(champion, created["state"]["champion_commit"])
        design = kernel.compile_design(task_id)
        self.assertIn("review-1", design["case_ids"])
        kernel.confirm(task_id, approve=True, selected_case_ids=("review-1",))
        kernel.dispatch(task_id)
        kernel.collect(task_id)
        first_round = kernel.analyze(task_id)
        self.assertEqual("await_user_confirmation", first_round["next_action"])
        self.assertEqual(["review-1"], first_round["failed_case_ids"])

        kernel.confirm(task_id, approve=True)
        candidate = kernel.optimize(task_id)
        self.assertIn("SKILL.md", candidate["changed_paths"])
        kernel.confirm(task_id, approve=True)
        published = kernel.publish_candidate(task_id)
        self.assertEqual(1, published["iteration"])
        self.assertEqual("a" * 40, published["state"]["challenger_commit"])

        kernel.dispatch(task_id)
        kernel.collect(task_id)
        next_round = kernel.analyze(task_id)
        self.assertEqual("verify_passes", next_round["next_action"])
        kernel.dispatch(
            task_id,
            purpose="pass-verification",
            case_ids=next_round["verification_required_case_ids"],
        )
        kernel.collect(task_id)
        final = kernel.analyze(task_id)

        comparison = final["candidate_comparison"]
        self.assertTrue(comparison["accepted"])
        self.assertTrue(comparison["case_set_comparable"])
        self.assertTrue(comparison["context_comparable"])
        self.assertEqual(["review-1"], comparison["improved_case_ids"])
        self.assertEqual([], comparison["hard_regression_case_ids"])
        self.assertEqual("promoted", final["candidate_outcome"]["status"])
        final_state = kernel.state(task_id)
        self.assertEqual("a" * 40, final_state["champion_commit"])
        self.assertIsNone(final_state["challenger_commit"])
        self.assertEqual("promoted", final_state["candidate_status"])
        snapshot = kernel.snapshot(task_id)
        self.assertEqual(2, len(snapshot["iterations"]))
        self.assertEqual(3, len(gateway.bindings))
        self.assertNotEqual(
            gateway.bindings[0].subject_hash,
            gateway.bindings[1].subject_hash,
        )
        self.assertEqual(
            gateway.bindings[1].subject_hash,
            gateway.bindings[2].subject_hash,
        )
        self.assertEqual(
            gateway.bindings[1].subject_hash,
            snapshot["iterations"][1]["environment_contract"]["skill"]["subject_hash"],
        )
        self.assertEqual(1, len(publisher.calls))

    def test_supplement_after_evaluation_starts_a_clean_iteration(self):
        failing_input = KernelInput.from_mapping(
            {
                "api_version": KERNEL_INPUT_API_VERSION,
                "skill_name": "reviewer",
                "goal": "Produce an evidence-backed review",
                "cases": [
                    {
                        "id": "review-1",
                        "prompt": "Review the fixture",
                        "expected_output": "grounded review",
                    }
                ],
            }
        )
        kernel = IterationKernel(
            self.root / "supplement-tasks",
            gateway_factory=lambda config: self.gateway,
            model_factory=lambda config: RepairModel(),
            publisher=FakePublisher(),
        )
        task_id = "supplement-task"
        kernel.create_task(self.config, failing_input, task_id=task_id)
        kernel.compile_design(task_id)
        kernel.dispatch(task_id)
        kernel.collect(task_id)
        decision = kernel.analyze(task_id)
        if decision["next_action"] == "verify_passes":
            kernel.dispatch(task_id, purpose="pass-verification", case_ids=decision["verification_required_case_ids"])
            kernel.collect(task_id)
            decision = kernel.analyze(task_id)
        self.assertEqual("await_user_confirmation", decision["next_action"])
        supplemented = KernelInput.from_mapping({
            "api_version": KERNEL_INPUT_API_VERSION,
            "skill_name": "reviewer",
            "goal": "Produce an evidence-backed review and explain severity",
            "standards": ["Every finding has a source location"],
            "cases": [{"id": "review-2", "prompt": "Review the fixture and rank severity", "expected_output": "ok"}],
        })
        kernel.confirm(task_id, approve=True, supplement=supplemented)
        state = kernel.state(task_id)
        self.assertEqual(1, state["iteration"])
        self.assertEqual("design_ready", state["phase"])
        self.assertIsNone(state["environment_contract_hash"])
        batch = kernel.dispatch(task_id)
        self.assertEqual("running", batch["status"])
        self.assertEqual("remote_running", kernel.state(task_id)["phase"])

    def test_human_confirmed_semantic_case_can_authorize_iteration(self):
        exploratory = KernelInput.from_mapping(
            {
                "api_version": KERNEL_INPUT_API_VERSION,
                "skill_name": "reviewer",
                "goal": "Find unsupported review conclusions",
            }
        )
        kernel = IterationKernel(
            self.root / "calibrated-tasks",
            gateway_factory=lambda config: self.gateway,
            model_factory=lambda config: RepairModel(),
        )
        task_id = "human-calibration"
        kernel.create_task(self.config, exploratory, task_id=task_id)
        design = kernel.compile_design(task_id)
        case_id = design["case_ids"][0]
        original_path = Path(kernel.state(task_id)["active_design"])
        original_bytes = original_path.read_bytes()

        approved = kernel.confirm(
            task_id,
            approve=True,
            selected_case_ids=(case_id,),
            case_calibrations={
                case_id: [
                    "结论必须引用实际读取到的源码位置",
                    "没有证据时明确说明无法判断",
                ]
            },
        )

        self.assertEqual("evaluation_ready", approved["phase"])
        self.assertEqual(original_bytes, original_path.read_bytes())
        active_case = kernel.snapshot(task_id)["design"]["cases"][0]
        test = active_case["metadata"]["aceval_test"]
        self.assertEqual("human_confirmed", test["oracle_trust"])
        self.assertTrue(test["oracle_ready"])
        self.assertEqual("semantic", active_case["metadata"]["expectation_mode"])
        self.assertNotEqual(design["cases"][0]["content_hash"], active_case["content_hash"])

        kernel.dispatch(task_id)
        kernel.collect(task_id)
        decision = kernel.analyze(task_id)
        self.assertEqual("await_user_confirmation", decision["next_action"])
        self.assertEqual([case_id], decision["optimization_eligible_case_ids"])
        self.assertEqual([case_id], decision["authorizable_failure_case_ids"])

    def test_kernel_optimizes_approved_scripts_and_references_together(self):
        (self.skill / "scripts").mkdir()
        (self.skill / "references").mkdir()
        (self.skill / "scripts" / "check.py").write_text("def check(evidence):\n    return False\n", encoding="utf-8")
        (self.skill / "references" / "rules.md").write_text("# Rules\n\nMissing evidence means pass.\n", encoding="utf-8")
        failing_input = KernelInput.from_mapping({
            "api_version": KERNEL_INPUT_API_VERSION,
            "skill_name": "reviewer",
            "cases": [{"id": "resource-1", "prompt": "Check evidence", "expected_output": "expected"}],
        })
        kernel = IterationKernel(
            self.root / "multi-file-tasks",
            gateway_factory=lambda config: self.gateway,
            model_factory=lambda config: MultiFileRepairModel(),
            publisher=FakePublisher(),
        )
        kernel.create_task(self.config, failing_input, task_id="multi-file-task")
        kernel.compile_design("multi-file-task")
        kernel.dispatch("multi-file-task")
        kernel.collect("multi-file-task")
        decision = kernel.analyze("multi-file-task")

        self.assertEqual("await_user_confirmation", decision["next_action"])
        self.assertEqual(["scripts/check.py", "references/rules.md"], decision["target_scope"])
        kernel.confirm("multi-file-task", approve=True)
        candidate = kernel.optimize("multi-file-task")
        self.assertEqual(["references/rules.md", "scripts/check.py"], candidate["changed_paths"])
        self.assertIn("return bool(evidence)", Path(candidate["path"], "scripts/check.py").read_text())
        self.assertIn("not evaluable", Path(candidate["path"], "references/rules.md").read_text())

    def test_custom_evalpack_is_attached_without_exposing_holdout(self):
        pack_path = Path(__file__).resolve().parents[1] / "evalpacks" / "csv-summary-smoke"
        custom_input = KernelInput.from_mapping(
            {
                "api_version": KERNEL_INPUT_API_VERSION,
                "skill_name": "reviewer",
                "goal": "Exercise a reviewed custom evaluation",
                "evalpack_path": str(pack_path),
            }
        )
        kernel = IterationKernel(
            self.root / "custom-tasks",
            gateway_factory=lambda config: self.gateway,
            model_factory=lambda config: self.model,
        )
        kernel.create_task(self.config, custom_input, task_id="custom-pack-task")
        design = kernel.compile_design("custom-pack-task")
        self.assertEqual("custom", design["evalpack"]["source"])
        self.assertEqual(str(pack_path), design["evalpack"]["artifacts"]["evaluation_suite"])
        self.assertTrue(design["case_ids"])
        self.assertTrue(all(case.get("metadata", {}).get("eval_split") != "holdout" for case in design["cases"] if case.get("metadata", {}).get("source") == "custom_evalpack"))
        event_types = [event["type"] for event in kernel.store.events("custom-pack-task")]
        self.assertIn("evaluation.evalpack_ready", event_types)
        self.assertEqual(len(design["case_ids"]), event_types.count("evaluation.case_path_ready"))

    def test_missing_local_path_is_materialized_by_the_checkout_boundary(self):
        checkout = FakeCheckoutManager(self.skill)
        remote_only = replace(
            self.config,
            skill_repository=replace(self.config.skill_repository, local_path=None),
        )
        kernel = IterationKernel(
            self.root / "checkout-tasks",
            gateway_factory=lambda config: self.gateway,
            model_factory=lambda config: self.model,
            checkout_manager=checkout,
        )
        created = kernel.create_task(remote_only, self.user_input, task_id="checkout-task-001")
        self.assertEqual("created", created["state"]["phase"])
        self.assertEqual(1, len(checkout.calls))
        self.assertEqual(str(self.skill.resolve()), kernel.snapshot("checkout-task-001")["config"]["skill_repository"]["local_path"])

    def test_automatic_evalpack_is_reused_by_signature_across_tasks(self):
        first = self.kernel.create_task(self.config, self.user_input, task_id="reuse-task-001")
        self.assertEqual("created", first["state"]["phase"])
        generated = self.kernel.compile_design("reuse-task-001")
        self.assertEqual("generated", generated["evalpack"]["source"])
        second = self.kernel.create_task(self.config, self.user_input, task_id="reuse-task-002")
        reused = self.kernel.compile_design("reuse-task-002")
        self.assertEqual("reused", reused["evalpack"]["source"])
        self.assertEqual(generated["evalpack"]["signature"], reused["evalpack"]["signature"])
        self.assertNotEqual(first["state"]["evaluation_flow_id"], second["state"]["evaluation_flow_id"])
        first_branches = {case["metadata"]["evaluation_branch"] for case in generated["cases"]}
        second_branches = {case["metadata"]["evaluation_branch"] for case in reused["cases"]}
        self.assertTrue(first_branches)
        self.assertTrue(second_branches)
        self.assertTrue(first_branches.isdisjoint(second_branches))

    def test_one_button_run_stops_only_at_a_real_gate(self):
        self.kernel.create_task(self.config, self.user_input, task_id="one-button-task")
        result = self.kernel.run_until_gate("one-button-task")
        self.assertEqual("design_ready", result["gate"])
        self.assertEqual(0, self.gateway.counter)
        approved = self.kernel.confirm(
            "one-button-task", approve=True, selected_case_ids=("review-1",)
        )
        self.assertEqual("evaluation_ready", approved["phase"])
        result = self.kernel.run_until_gate("one-button-task")
        self.assertEqual("converged", result["gate"])
        self.assertEqual("converged", result["snapshot"]["state"]["phase"])
        self.assertGreaterEqual(self.gateway.counter, 2)

    def test_unready_generated_cases_can_run_but_cannot_authorize_mutation(self):
        exploratory = KernelInput.from_mapping({
            "api_version": KERNEL_INPUT_API_VERSION,
            "skill_name": "reviewer",
            "goal": "Explore weaknesses without a trusted expected result",
        })
        kernel = IterationKernel(
            self.root / "unready-pack-tasks",
            gateway_factory=lambda config: self.gateway,
            model_factory=lambda config: RepairModel(),
        )
        kernel.create_task(self.config, exploratory, task_id="unready-pack-task")
        design = kernel.compile_design("unready-pack-task")
        self.assertNotEqual("ready", design["evalpack"]["status"])
        kernel.dispatch("unready-pack-task")
        kernel.collect("unready-pack-task")
        decision = kernel.analyze("unready-pack-task")
        self.assertEqual("needs_evidence", decision["next_action"])
        self.assertEqual([], decision["optimization_eligible_case_ids"])
        self.assertEqual([], decision["proposed_changes"])
        reopened = kernel.reopen_case_review("unready-pack-task")
        self.assertEqual("design_ready", reopened["phase"])
        self.assertFalse(reopened["design_approved"])

    def test_invalid_task_id_is_rejected_before_checkout_path_is_used(self):
        checkout = FakeCheckoutManager(self.skill)
        remote_only = replace(self.config, skill_repository=replace(self.config.skill_repository, local_path=None))
        kernel = IterationKernel(
            self.root / "unsafe-id-tasks",
            gateway_factory=lambda config: self.gateway,
            model_factory=lambda config: self.model,
            checkout_manager=checkout,
        )
        with self.assertRaises(Exception):
            kernel.create_task(remote_only, self.user_input, task_id="../../escape")
        self.assertEqual([], checkout.calls)


if __name__ == "__main__":
    unittest.main()
