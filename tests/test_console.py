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


if __name__ == "__main__":
    unittest.main()
