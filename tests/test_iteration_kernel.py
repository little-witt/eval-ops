import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from aceval.agent_runtime import ModelReply
from aceval.iteration_kernel import IterationKernel
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

    def test_failed_cases_require_confirmation_before_candidate_and_next_round(self):
        publisher = FakePublisher()
        kernel = IterationKernel(
            self.root / "repair-tasks",
            gateway_factory=lambda config: self.gateway,
            model_factory=lambda config: RepairModel(),
            publisher=publisher,
        )
        kernel.create_task(self.config, self.user_input, task_id="repair-task-001")
        kernel.compile_design("repair-task-001")
        kernel.dispatch("repair-task-001")
        kernel.collect("repair-task-001")
        decision = kernel.analyze("repair-task-001")
        if decision["next_action"] == "verify_passes":
            kernel.dispatch("repair-task-001", purpose="pass-verification", case_ids=decision["verification_required_case_ids"])
            kernel.collect("repair-task-001")
            decision = kernel.analyze("repair-task-001")
        self.assertEqual("await_user_confirmation", decision["next_action"])
        with self.assertRaises(Exception):
            kernel.optimize("repair-task-001")
        kernel.confirm("repair-task-001", approve=True)
        candidate = kernel.optimize("repair-task-001")
        self.assertTrue(Path(candidate["path"], "SKILL.md").is_file())
        published = kernel.publish_candidate("repair-task-001")
        self.assertEqual(1, published["iteration"])
        self.assertEqual("design_ready", published["state"]["phase"])
        self.assertEqual(1, len(publisher.calls))

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
        self.kernel.create_task(self.config, self.user_input, task_id="reuse-task-002")
        reused = self.kernel.compile_design("reuse-task-002")
        self.assertEqual("reused", reused["evalpack"]["source"])
        self.assertEqual(generated["evalpack"]["signature"], reused["evalpack"]["signature"])

    def test_one_button_run_stops_only_at_a_real_gate(self):
        self.kernel.create_task(self.config, self.user_input, task_id="one-button-task")
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
