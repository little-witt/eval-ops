import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from aceval.agent_runtime import ModelReply, ScriptedModelClient
from aceval.iteration_kernel import IterationKernel
from aceval.kernel_contracts import KERNEL_CONFIG_API_VERSION, KERNEL_INPUT_API_VERSION, KernelConfig, KernelInput
from aceval.skill_harness import CapabilityArchitect, SkillProjectBuilder, select_blueprint, validate_blueprint
from aceval.skill_tree_optimizer import SkillTreePatchPolicy


def proposal(proposal_id="capability-one", *, create=False):
    return {
        "id": proposal_id,
        "title": "Evidence summary",
        "problem": "Users need a reusable evidence summary.",
        "user_value": "Reduce repeated manual summarization.",
        "triggers": ["summarize the evidence"],
        "inputs": ["evidence records"],
        "outputs": ["grounded summary"],
        "workflow": ["Read evidence", "Validate fields", "Return summary"],
        "dependencies": [],
        "acceptance_criteria": ["Every conclusion cites supplied evidence"],
        "evidence": ["The requested capability or repeated workflow justifies the proposal"],
        "risks": ["Do not invent missing evidence"],
        "planned_files": ([
            {"path": "SKILL.md", "action": "create", "reason": "Skill entrypoint"},
            {"path": "references/evidence.md", "action": "create", "reason": "Detailed reusable rules"},
        ] if create else [
            {"path": "SKILL.md", "action": "modify", "reason": "Route the new capability"},
            {"path": "references/evidence.md", "action": "create", "reason": "Detailed reusable rules"},
        ]),
        "cases": [
            {"id": "%s-positive" % proposal_id, "kind": "capability", "prompt": "Summarize these evidence records", "expected_output": "grounded"},
            {"id": "%s-boundary" % proposal_id, "kind": "boundary", "prompt": "Summarize an empty evidence set", "expected_output": "not_evaluable"},
            {"id": "%s-near-miss" % proposal_id, "kind": "negative_trigger", "prompt": "Write a fictional story"},
        ] + ([] if create else [
            {"id": "%s-regression" % proposal_id, "kind": "regression", "prompt": "Run the existing primary workflow", "expected_output": "grounded"},
        ]),
    }


def blueprint_payload(operation="extend", proposals=None):
    values = proposals or [proposal(create=operation == "create")]
    return {
        "goal": "Provide grounded evidence summaries",
        "summary": "Add a focused evidence capability with regression protection.",
        "proposals": values,
        "recommended_proposal_ids": [values[0]["id"]],
    }


class HarnessGateway:
    def __init__(self):
        self.index = 0
        self.requests = []

    def start_session(self, request):
        self.index += 1
        self.requests.append(dict(request))
        return "session-%d" % self.index

    def poll_session(self, session_id):
        return {"status": "COMPLETED", "session_id": session_id}

    def fetch_session(self, session_id):
        return {
            "schema_version": "aceval.imported-session/v1",
            "session_id": session_id,
            "completeness": {"trace": True, "output": True},
            "observation": {
                "output": "wrong",
                "trace": [{"kind": "tool_call", "name": "read", "path": "SKILL.md"}],
                "metadata": {}, "usage": {"total_tokens": 10}, "error": None,
            },
        }


class HarnessModel:
    def __init__(self, operation):
        self.operation = operation
        self.requests = []

    def complete(self, messages, tools):
        request = json.loads(messages[-1]["content"])
        self.requests.append(request)
        if "existing_resource_inventory" in request:
            if self.operation == "discover":
                proposals = [proposal("capability-one"), proposal("capability-two")]
                proposals[1]["title"] = "Evidence validation"
                return ModelReply(content=json.dumps(blueprint_payload("discover", proposals)))
            return ModelReply(content=json.dumps(blueprint_payload(self.operation)))
        if "approved_create_paths" in request and "skill_name" in request:
            self.assert_builder_has_no_cases(request)
            files = [
                {
                    "path": "SKILL.md",
                    "content": "---\nname: new-skill\ndescription: Use this Skill whenever users ask for grounded evidence summaries.\n---\n\n# Evidence Skill\n\n## Inputs\n\n- Evidence records\n\n## Outputs\n\n- Grounded summary\n\n## Workflow\n\n1. Read the evidence.\n2. Follow `references/evidence.md`.\n3. Return only supported conclusions.\n",
                    "reason": "entrypoint",
                },
                {"path": "references/evidence.md", "content": "# Evidence rules\n\nCite every conclusion.\n", "reason": "progressive disclosure"},
            ]
            return ModelReply(content=json.dumps({"files": files, "rationale": "Initial reusable Skill"}))
        if "current_files" in request:
            return ModelReply(content=json.dumps({
                "changes": [
                    {"path": "SKILL.md", "operation": "replace_text", "old_text": "2. Return a grounded result.", "new_text": "2. Read `references/evidence.md` and return a grounded result.", "reason": "route capability"},
                    {"path": "references/evidence.md", "operation": "create_file", "content": "# Evidence rules\n\nCite every conclusion.\n", "reason": "new detailed rules"},
                ],
                "rationale": "Add the approved capability without bloating the entrypoint.",
            }))
        return ModelReply(content=json.dumps({
            "case_results": [{"case_id": case["id"], "status": "fail", "verification_status": "not_run", "reason": "capability is absent", "evidence_refs": []} for case in request["cases"]],
            "failure_clusters": [{"id": "missing-capability", "case_ids": [case["id"] for case in request["cases"]], "root_cause": "approved evidence capability is not implemented", "skill_change_authorized": True}],
            "conflicts": [],
            "proposed_changes": [{"target": "references/evidence.md", "change": "add detailed evidence rules", "why": "implement approved capability", "case_ids": [case["id"] for case in request["cases"]]}],
            "target_scope": ["SKILL.md", "references/evidence.md"],
        }))

    @staticmethod
    def assert_builder_has_no_cases(request):
        if len(request["blueprint"]["proposals"]) != 1:
            raise AssertionError("builder received unselected capability proposals")
        for item in request["blueprint"]["proposals"]:
            if "cases" in item:
                raise AssertionError("builder received frozen evaluation cases")


class ApplyingPublisher:
    def __init__(self):
        self.calls = []

    def publish(self, repository, candidate_root, *, message):
        self.calls.append((repository, candidate_root, message))
        manifest = json.loads((Path(candidate_root) / "candidate.manifest.json").read_text())
        root = Path(repository.local_path)
        for relative in manifest["changed_paths"]:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(candidate_root) / relative, target)
        return "b" * 40


class SkillHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, skill):
        return KernelConfig.from_mapping({
            "api_version": KERNEL_CONFIG_API_VERSION,
            "skill_repository": {"ssh_url": "ssh://git@git.example/new-skill.git", "branch": "feature/harness", "local_path": str(skill), "authorization_token_env": "REPO_PAT", "mount_path": "/workspace/skill"},
            "code_repository": None,
            "local_analysis": {"model_command": ["unused"], "model_id": "fake"},
            "remote_agent": {"profile_path": str(self.root / "profile.json"), "poll_interval_seconds": 1},
            "policy": {"max_generated_cases": 4, "max_rounds": 3},
        })

    def existing_skill(self):
        skill = self.root / "existing"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# Existing Skill\n\n## Inputs\n\n- Evidence\n\n## Outputs\n\n- Result\n\n## Workflow\n\n1. Read evidence.\n2. Return a grounded result.\n", encoding="utf-8")
        return skill

    def test_intent_modes_are_independent_from_input_richness(self):
        explicit = KernelInput.from_mapping({"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "demo", "operation": "extend", "capabilities": ["Add evidence summaries"]})
        discover = KernelInput.from_mapping({"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "demo"})
        create = KernelInput.from_mapping({"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "demo", "goal": "Create summaries"})
        self.assertEqual("extend", explicit.intent_mode(has_existing_skill=True))
        self.assertEqual("discover", discover.intent_mode(has_existing_skill=True))
        self.assertEqual("create", create.intent_mode(has_existing_skill=False))

    def test_empty_greenfield_intent_is_rejected_instead_of_guessing_a_skill(self):
        empty = KernelInput.from_mapping({"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "demo"})
        with self.assertRaisesRegex(Exception, "creating a Skill requires"):
            empty.intent_mode(has_existing_skill=False)

    def test_discovery_requires_selection_before_it_becomes_extension(self):
        skill = self.existing_skill()
        model = HarnessModel("discover")
        input_value = KernelInput.from_mapping({"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "existing", "operation": "discover"})
        architect = CapabilityArchitect(model)
        blueprint = architect.plan(operation="discover", user_input=input_value, existing_paths=("SKILL.md",), current_skill=(skill / "SKILL.md").read_text(), policy=SkillTreePatchPolicy())
        selected = select_blueprint(blueprint, ("capability-two",))
        self.assertEqual([], blueprint["selected_proposal_ids"])
        self.assertEqual("extend", selected["operation"])
        self.assertEqual(["capability-two"], selected["selected_proposal_ids"])

    def test_greenfield_builder_creates_only_approved_files_and_hides_cases(self):
        skill = self.root / "empty"
        skill.mkdir()
        model = HarnessModel("create")
        input_value = KernelInput.from_mapping({"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "new-skill", "operation": "create", "goal": "Provide grounded summaries"})
        blueprint = CapabilityArchitect(model).plan(operation="create", user_input=input_value, existing_paths=(), current_skill=None, policy=SkillTreePatchPolicy())
        candidate = SkillProjectBuilder(model).build(subject=skill, blueprint=blueprint, skill_name="new-skill", output_root=self.root / "candidates", policy=SkillTreePatchPolicy())
        self.assertEqual(("SKILL.md", "references/evidence.md"), candidate.created_paths)
        self.assertTrue((candidate.path / "SKILL.md").is_file())
        self.assertFalse((skill / "SKILL.md").exists())

    def test_greenfield_builder_never_receives_unselected_capability_proposals(self):
        skill = self.root / "selective-empty"
        skill.mkdir()
        first = proposal("capability-one", create=True)
        second = proposal("capability-two", create=True)
        second["planned_files"][1]["path"] = "references/other.md"
        raw = blueprint_payload("create", [first, second])
        blueprint = validate_blueprint(raw, operation="create", existing_paths=(), policy=SkillTreePatchPolicy())
        model = HarnessModel("create")

        candidate = SkillProjectBuilder(model).build(
            subject=skill,
            blueprint=blueprint,
            skill_name="new-skill",
            output_root=self.root / "selective-candidates",
            policy=SkillTreePatchPolicy(),
        )

        self.assertEqual(("SKILL.md", "references/evidence.md"), candidate.created_paths)

    def test_kernel_create_blueprint_build_review_publish_then_compile(self):
        skill = self.root / "new"
        skill.mkdir()
        model = HarnessModel("create")
        publisher = ApplyingPublisher()
        gateway = HarnessGateway()
        kernel = IterationKernel(self.root / "create-tasks", model_factory=lambda config: model, gateway_factory=lambda config: gateway, publisher=publisher)
        user_input = KernelInput.from_mapping({"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "new-skill", "operation": "create", "goal": "Provide grounded summaries"})
        kernel.create_task(self.config(skill), user_input, task_id="create-task")

        gate = kernel.run_until_gate("create-task")
        self.assertEqual("blueprint_ready", gate["gate"])
        kernel.confirm("create-task", approve=True)
        candidate = kernel.advance("create-task")
        self.assertTrue(candidate["initial_build"])
        self.assertEqual("initial_candidate_ready", kernel.state("create-task")["phase"])
        self.assertFalse((skill / "SKILL.md").exists())
        kernel.confirm("create-task", approve=True)
        published = kernel.advance("create-task")
        self.assertTrue(published["initial_build"])
        design = kernel.advance("create-task")
        self.assertIn("capability-one-positive", design["case_ids"])
        self.assertTrue((skill / "SKILL.md").is_file())
        baseline = kernel.advance("create-task")
        self.assertEqual("without-skill-baseline", baseline["purpose"])
        self.assertEqual("baseline_running", kernel.state("create-task")["phase"])
        self.assertTrue(all("不得读取或使用" in request["prompt"] for request in gateway.requests))
        kernel.advance("create-task")
        self.assertEqual("baseline_collected", kernel.state("create-task")["phase"])
        evaluation = kernel.advance("create-task")
        self.assertEqual("evaluation", evaluation["purpose"])

    def test_kernel_extension_freezes_cases_then_creates_approved_resource(self):
        skill = self.existing_skill()
        model = HarnessModel("extend")
        kernel = IterationKernel(self.root / "extend-tasks", model_factory=lambda config: model, gateway_factory=lambda config: HarnessGateway(), publisher=ApplyingPublisher())
        user_input = KernelInput.from_mapping({"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "existing", "operation": "extend", "capabilities": ["Add grounded evidence summaries"]})
        kernel.create_task(self.config(skill), user_input, task_id="extend-task")
        self.assertEqual("blueprint_ready", kernel.run_until_gate("extend-task")["gate"])
        kernel.confirm("extend-task", approve=True)
        design = kernel.compile_design("extend-task")
        self.assertIn("capability-one-positive", design["case_ids"])
        kernel.dispatch("extend-task")
        kernel.collect("extend-task")
        decision = kernel.analyze("extend-task")
        self.assertEqual("await_user_confirmation", decision["next_action"])
        kernel.confirm("extend-task", approve=True)
        candidate = kernel.optimize("extend-task")
        self.assertEqual(["references/evidence.md"], candidate["created_paths"])
        self.assertIn("SKILL.md", candidate["changed_paths"])


if __name__ == "__main__":
    unittest.main()
