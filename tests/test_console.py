import json
import tempfile
import unittest
from pathlib import Path

from aceval.console import build_console
from aceval.iteration_kernel import KERNEL_STATE_API_VERSION
from aceval.kernel_contracts import KERNEL_CONFIG_API_VERSION, KERNEL_INPUT_API_VERSION
from aceval.optimization_graph import (
    OPTIMIZATION_GRAPH_API_VERSION,
    compile_optimization_graph,
)
from aceval.task_center import TaskStore


class OptimizationConsoleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "history.json").write_text(
            json.dumps(
                {
                    "skill_name": "reviewer",
                    "current_best": "v0",
                    "iterations": [
                        {
                            "version": "v0",
                            "parent": None,
                            "expectation_pass_rate": 0.8,
                            "grading_result": "baseline",
                            "is_current_best": True,
                        },
                        {
                            "version": "v1",
                            "parent": "v0",
                            "expectation_pass_rate": 0.6,
                            "grading_result": "lost",
                            "is_current_best": False,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.plan = self.workspace / "optimization-plan.json"
        self.plan.write_text(
            json.dumps(
                {
                    "goal": "Improve review quality",
                    "repetitions": 1,
                    "skill_ref": "feature/test",
                    "success_criteria": ["Strict JSON"],
                    "promotion_rules": ["No quality regression"],
                    "iterations": [
                        {"version": "v0", "title": "Baseline"},
                        {
                            "version": "v1",
                            "title": "Format gate",
                            "hypothesis": "Formatting is unstable",
                            "changes": ["Move the gate to the top"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.profile = self.root / "profile.json"
        self.profile.write_text(
            json.dumps(
                {
                    "name": "local",
                    "base_url": "https://user:password@example.test/v1",
                    "agent": "$CATX_AGENT_ID",
                    "environment_id": "$CATX_ENV_ID",
                    "credentials_file": "secrets.json",
                    "repositories": [
                        {
                            "type": "repository",
                            "url": "ssh://git@example.test/org/repo.git",
                            "mount_path": "/workspace/repo",
                            "authorization_token": "actual-secret-token",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self._write_iteration(1, "old_skill", 0.8, formal=False, tokens=300)
        self._write_iteration(2, "old_skill", 0.8, formal=False, tokens=300)
        self._write_iteration(2, "with_skill", 0.6, formal=True, tokens=120)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_iteration(self, number, configuration, rate, *, formal, tokens):
        eval_dir = self.workspace / f"iteration-{number}" / "eval-01-ts-web-case"
        eval_dir.mkdir(parents=True, exist_ok=True)
        (eval_dir / "eval_metadata.json").write_text(
            json.dumps(
                {
                    "eval_id": 1,
                    "eval_name": "ts-web-case",
                    "stack": "typescript-web",
                    "case_type": "defect",
                    "prompt": "Review the change",
                    "assertions": ["Strict JSON"],
                }
            ),
            encoding="utf-8",
        )
        run = eval_dir / configuration / "run-1"
        run.mkdir(parents=True)
        (run / "request.json").write_text("{}", encoding="utf-8")
        (run / "grading.json").write_text(
            json.dumps(
                {
                    "summary": {"pass_rate": rate},
                    "formal_grade": {"status": "pass" if formal else "fail"},
                    "binding_verified": True,
                    "expectations": [
                        {"text": "Strict JSON", "passed": formal, "evidence": "result"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (run / "timing.json").write_text(
            json.dumps({"total_tokens": tokens, "total_duration_seconds": 10}),
            encoding="utf-8",
        )
        (run / "binding.json").write_text(
            json.dumps(
                {
                    "verified": True,
                    "skill_commit": ("a" if configuration == "old_skill" else "b") * 40,
                    "session_id": "session-1",
                }
            ),
            encoding="utf-8",
        )

    def test_compiles_convergence_tree_and_redacts_profile(self):
        graph = compile_optimization_graph(
            self.workspace, plan_path=self.plan, profile_path=self.profile
        )
        self.assertEqual(OPTIMIZATION_GRAPH_API_VERSION, graph["api_version"])
        self.assertEqual("do_not_promote", graph["convergence"]["decision"])
        self.assertEqual("v0", graph["convergence"]["current_best"])
        self.assertEqual(
            ["input", "evaluation-design", "v0", "v1", "convergence"],
            [item["id"] for item in graph["nodes"]],
        )
        self.assertEqual("improved", graph["dimension_summary"][0]["status"])
        serialized = json.dumps(graph)
        self.assertNotIn("actual-secret-token", serialized)
        self.assertNotIn("password@", serialized)
        self.assertIn("CATX_AGENT_ID", serialized)
        case = graph["evaluation_design"]["cases"][0]
        self.assertEqual("TypeScript Web", case["group"])
        self.assertEqual("defect", case["type"])

    def test_legacy_case_ids_do_not_invent_group_or_type(self):
        metadata = self.workspace / "iteration-2" / "eval-01-ts-web-case" / "eval_metadata.json"
        value = json.loads(metadata.read_text(encoding="utf-8"))
        value.pop("stack", None)
        value.pop("case_type", None)
        metadata.write_text(json.dumps(value), encoding="utf-8")

        graph = compile_optimization_graph(self.workspace, plan_path=self.plan)
        case = graph["evaluation_design"]["cases"][0]
        self.assertEqual("未分组", case["group"])
        self.assertEqual("未标注", case["type"])

    def test_builds_offline_console_with_embedded_graph(self):
        output = self.root / "console"
        task_root = self.root / "tasks"
        TaskStore(task_root).create(
            task_id="review-task-001",
            skill_name="reviewer",
            skill_source="/skills/reviewer",
            scenario="code-review",
            goal="Improve review quality",
            standards=("Strict JSON",),
        )
        kernel_root = task_root / "review-task-001" / "kernel"
        kernel_root.mkdir()
        (kernel_root / "config.json").write_text(json.dumps({
            "api_version": KERNEL_CONFIG_API_VERSION,
            "skill_repository": {"ssh_url": "ssh://git@example.test/reviewer.git", "branch": "feature/test", "local_path": str(self.workspace), "authorization_token_env": "REPO_PAT", "mount_path": "/workspace/skill"},
            "code_repository": None,
            "local_analysis": {"model_command": ["model-bridge", "--api-key", "actual-model-secret"], "model_id": "fake"},
            "remote_agent": {"profile_path": str(self.profile)},
            "policy": {},
        }), encoding="utf-8")
        (kernel_root / "input.json").write_text(json.dumps({
            "api_version": KERNEL_INPUT_API_VERSION,
            "skill_name": "reviewer",
            "operation": "extend",
            "goal": "Improve review quality",
            "capabilities": ["Review evidence quality"],
        }), encoding="utf-8")
        (kernel_root / "state.json").write_text(json.dumps({
            "api_version": KERNEL_STATE_API_VERSION,
            "phase": "blueprint_ready",
            "intent_mode": "extend",
            "iteration": 0,
            "revision": 1,
            "blueprint": None,
        }), encoding="utf-8")
        result = build_console(
            self.workspace,
            output,
            plan_path=self.plan,
            profile_path=self.profile,
            task_root=task_root,
        )
        self.assertEqual("static", result["mode"])
        for name in (
            "index.html",
            "styles.css",
            "app.js",
            "optimization-graph.json",
            "optimization-graph.js",
            "task-center.js",
        ):
            self.assertTrue((output / name).is_file(), name)
        embedded = (output / "optimization-graph.js").read_text(encoding="utf-8")
        self.assertIn("aceval.optimization-graph/v1", embedded)
        self.assertNotIn("actual-secret-token", embedded)
        task_center = (output / "task-center.js").read_text()
        self.assertIn("review-task-001", task_center)
        self.assertIn('"kernel"', task_center)
        self.assertIn('"intent_mode": "extend"', task_center)
        self.assertNotIn("actual-model-secret", task_center)
        self.assertIn("<2 arguments hidden>", task_center)

    def test_graph_exposes_authored_path_trace_and_score_vector(self):
        """The read model keeps process conformance separate from outcome score."""
        path = {
            "api_version": "aceval.execution-path-spec/v1",
            "purpose": "读取 Skill 后完成审查",
            "trace_completeness_required": True,
            "steps": [
                {
                    "id": "load",
                    "label": "读取 Skill",
                    "kind": "required",
                    "match": {"event_type": "tool_call", "contains": "SKILL.md"},
                    "after": [],
                },
                {
                    "id": "publish",
                    "label": "不得发布 Skill",
                    "kind": "forbidden",
                    "match": {
                        "contains": [
                            "git push",
                            "https://user:actual-url-secret@example.test/repo.git",
                            "Authorization: Basic YWN0dWFsLXBhdGgtc2VjcmV0",
                        ],
                        "authorization_token": "actual-path-secret",
                    },
                    "after": ["load"],
                },
            ],
        }
        (self.workspace / "execution-paths.json").write_text(
            json.dumps({"paths": {"ts-web-case": path}}), encoding="utf-8"
        )
        run = self.workspace / "iteration-2" / "eval-01-ts-web-case" / "with_skill" / "run-1"
        (run / "path_conformance.json").write_text(
            json.dumps({"status": "pass", "coverage": 1.0, "steps": [
                {"id": "load", "observed": True},
                {"id": "publish", "observed": False},
            ]}), encoding="utf-8"
        )
        (run / "outputs").mkdir(exist_ok=True)
        (run / "outputs" / "session.json").write_text(
            json.dumps({"completeness": {"trace": True}, "observation": {
                "trace": [{"kind": "tool_call", "tool": "read_file"}, {"kind": "tool_result"}],
                "metadata": {},
            }}), encoding="utf-8"
        )
        (run / "outputs" / "attempts.json").write_text(
            json.dumps([{"attempt": 1}]), encoding="utf-8"
        )
        graph = compile_optimization_graph(self.workspace, plan_path=self.plan)
        case = graph["evaluation_design"]["cases"][0]
        self.assertEqual(2, case["path_graph"]["step_count"])
        self.assertIn("flowchart TD", case["path_graph"]["mermaid"])
        self.assertIn("-->", case["path_graph"]["mermaid"])
        self.assertEqual("pass", case["candidate"]["path_status"])
        self.assertTrue(case["candidate"]["trace_complete"])
        self.assertEqual(0, case["candidate"]["retry_count"])
        self.assertIn("outcome", case["candidate"]["score_dimensions"])
        self.assertEqual("pass", case["candidate"]["score_dimensions"]["evidence"]["status"])
        self.assertFalse(case["candidate"]["score"]["hard_gate"])
        self.assertNotIn("actual-path-secret", json.dumps(case["path"]))
        self.assertNotIn("actual-url-secret", json.dumps(case["path"]))
        self.assertNotIn("YWN0dWFsLXBhdGgtc2VjcmV0", json.dumps(case["path"]))
        self.assertIn("path_analysis", graph)

    def test_incomplete_trace_blocks_hard_gate_even_with_perfect_outcome(self):
        run = self.workspace / "iteration-2" / "eval-01-ts-web-case" / "with_skill" / "run-1"
        (run / "grading.json").write_text(
            json.dumps(
                {
                    "summary": {"pass_rate": 1.0},
                    "formal_grade": {"status": "pass"},
                    "binding_verified": True,
                    "expectations": [
                        {"text": "Strict JSON", "passed": True, "evidence": "result"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (run / "outputs").mkdir(exist_ok=True)
        (run / "outputs" / "session.json").write_text(
            json.dumps(
                {
                    "completeness": {"trace": False},
                    "observation": {
                        "trace": [{"kind": "tool_call", "tool": "read_file"}],
                        "metadata": {"trace_may_be_truncated": True},
                    },
                }
            ),
            encoding="utf-8",
        )
        graph = compile_optimization_graph(self.workspace, plan_path=self.plan)
        candidate = graph["evaluation_design"]["cases"][0]["candidate"]
        self.assertEqual(1.0, candidate["pass_rate"])
        self.assertFalse(candidate["trace_complete"])
        self.assertFalse(candidate["score"]["hard_gate"])

    def test_missing_trace_completeness_remains_unmeasured(self):
        run = self.workspace / "iteration-2" / "eval-01-ts-web-case" / "with_skill" / "run-1"
        (run / "grading.json").write_text(
            json.dumps(
                {
                    "summary": {"pass_rate": 1.0},
                    "formal_grade": {"status": "pass"},
                    "binding_verified": True,
                    "expectations": [{"text": "Strict JSON", "passed": True}],
                }
            ),
            encoding="utf-8",
        )
        (run / "outputs").mkdir(exist_ok=True)
        (run / "outputs" / "session.json").write_text(
            json.dumps(
                {
                    "observation": {
                        "trace": [{"kind": "tool_call", "tool": "read_file"}],
                        "metadata": {},
                    }
                }
            ),
            encoding="utf-8",
        )
        graph = compile_optimization_graph(self.workspace, plan_path=self.plan)
        candidate = graph["evaluation_design"]["cases"][0]["candidate"]
        self.assertIsNone(candidate["trace_complete"])
        self.assertEqual(
            "not_measured", candidate["score_dimensions"]["evidence"]["status"]
        )

    def test_missing_pass_rate_remains_unknown_instead_of_zero(self):
        run = self.workspace / "iteration-2" / "eval-01-ts-web-case" / "with_skill" / "run-1"
        (run / "grading.json").write_text(
            json.dumps(
                {
                    "formal_grade": {"status": "pass"},
                    "binding_verified": True,
                    "expectations": [{"text": "Strict JSON", "passed": True}],
                }
            ),
            encoding="utf-8",
        )
        graph = compile_optimization_graph(self.workspace, plan_path=self.plan)
        candidate = graph["evaluation_design"]["cases"][0]["candidate"]
        self.assertIsNone(candidate["pass_rate"])
        self.assertIsNone(candidate["score"]["overall"])

    def test_string_false_binding_cannot_pass_the_binding_dimension(self):
        run = self.workspace / "iteration-2" / "eval-01-ts-web-case" / "with_skill" / "run-1"
        (run / "grading.json").write_text(
            json.dumps(
                {
                    "summary": {"pass_rate": 1.0},
                    "formal_grade": {"status": "pass"},
                    "binding_verified": "false",
                    "expectations": [{"text": "Strict JSON", "passed": "false"}],
                }
            ),
            encoding="utf-8",
        )
        graph = compile_optimization_graph(self.workspace, plan_path=self.plan)
        candidate = graph["evaluation_design"]["cases"][0]["candidate"]
        self.assertFalse(candidate["binding_verified"])
        self.assertEqual("fail", candidate["score_dimensions"]["binding"]["status"])
        self.assertEqual("fail", candidate["score_dimensions"]["format"]["status"])
        self.assertFalse(candidate["score"]["hard_gate"])

    def test_v3_graph_preserves_conditional_edges_without_inventing_node_order(self):
        (self.workspace / "execution-paths.json").write_text(
            json.dumps(
                {
                    "paths": {
                        "ts-web-case": {
                            "api_version": "aceval.path-analysis/v3",
                            "nodes": [
                                {
                                    "step_id": "call",
                                    "action": "调用工具",
                                    "requiredness": "required",
                                    "match": {"contains": "call"},
                                },
                                {
                                    "step_id": "retry",
                                    "action": "超时重试",
                                    "requiredness": "recommended",
                                    "match": {"contains": "retry"},
                                },
                                {
                                    "step_id": "finish",
                                    "action": "完成",
                                    "requiredness": "required",
                                    "match": {"contains": "finish"},
                                },
                            ],
                            "edges": [
                                {
                                    "from": "call",
                                    "to": "retry",
                                    "kind": "failure",
                                    "when": "exit_code=timeout",
                                }
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        graph = compile_optimization_graph(self.workspace, plan_path=self.plan)
        path_graph = graph["evaluation_design"]["cases"][0]["path_graph"]
        self.assertEqual(
            [
                {
                    "source": "call",
                    "target": "retry",
                    "relation": "failure",
                    "condition": "exit_code=timeout",
                }
            ],
            path_graph["edges"],
        )
        self.assertIn("failure · exit_code=timeout", path_graph["mermaid"])
        self.assertEqual(1, path_graph["mermaid"].count("-->"))


if __name__ == "__main__":
    unittest.main()
