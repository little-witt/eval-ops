import json
import tempfile
import unittest
from pathlib import Path

from aceval.task_center import TASK_EVENT_API_VERSION, TaskStore


class TaskCenterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name, "tasks")
        self.store = TaskStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_automatic_evaluation_is_user_invisible_but_customizable(self):
        task = self.store.create(
            task_id="d2c-home-001",
            skill_name="d2c-home",
            skill_source="ssh://git.example/skill.git@abc",
            scenario="d2c",
            goal="Restore the home page",
            standards=("Pixel difference <= 1%", "No console errors"),
            cases=({"id": "desktop", "viewport": [1440, 900]},),
            environment={"provider": "local-browser"},
        )
        self.assertEqual("automatic", task["evaluation"]["mode"])
        self.assertTrue(task["evaluation"]["customizable"])
        self.assertEqual("desktop", task["evaluation"]["cases"][0]["id"])
        events = self.store.events(task["id"])
        self.assertEqual(["task.created", "evaluation.design_generated"], [item["type"] for item in events])
        self.assertTrue(all(item["api_version"] == TASK_EVENT_API_VERSION for item in events))

    def test_custom_evaluation_and_append_only_iteration_events(self):
        task = self.store.create(
            task_id="review-task-001",
            skill_name="reviewer",
            skill_source="/skills/reviewer",
            scenario="code-review",
            goal="Find defects",
            standards=("No false positives",),
            evaluation_spec={"mode": "custom", "pack_ref": "packs/review-v1"},
        )
        event = self.store.append_event(
            task["id"], "iteration.planned", {"hypothesis": "Improve evidence"}, iteration=1
        )
        self.assertEqual(3, event["seq"])
        self.assertEqual(1, event["iteration"])
        self.assertEqual(1, len(self.store.list()))
        disk_events = (self.root / task["id"] / "events.jsonl").read_text().splitlines()
        self.assertEqual(3, len(disk_events))
        self.assertEqual("evaluation.design_attached", json.loads(disk_events[1])["type"])

    def test_iteration_and_decision_update_task_and_emit_convergence(self):
        task = self.store.create(
            task_id="optimize-task-001",
            skill_name="d2c",
            skill_source="/skills/d2c",
            scenario="d2c",
            goal="Reduce visual drift",
            standards=("diff <= 1%",),
        )
        planned = self.store.begin_iteration(
            task["id"],
            hypothesis="Spacing instruction is ambiguous",
            planned_changes=("Add an explicit spacing token",),
            target_dimensions=("visual fidelity",),
        )
        self.assertEqual(1, planned["iteration"])
        self.store.record_decision(
            task["id"],
            iteration=1,
            decision="converged",
            reason="All frozen cases pass",
            metrics={"visual_diff_ratio": 0},
        )
        current = self.store.load(task["id"])
        self.assertEqual("converged", current["status"])
        self.assertEqual(1, current["current_iteration"])
        self.assertEqual("task.converged", self.store.events(task["id"])[-1]["type"])


if __name__ == "__main__":
    unittest.main()
