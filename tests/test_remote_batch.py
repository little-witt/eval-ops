import json
import tempfile
import time
import unittest
from pathlib import Path

from aceval.remote_batch import RemoteBatchCoordinator, RemoteBatchError, compact_remote_prompt
from aceval.kernel_contracts import REMOTE_BATCH_API_VERSION
from aceval.task_center import TaskStore


class FakeGateway:
    def __init__(self):
        self.started = []
        self.polls = []

    def start_session(self, request):
        self.started.append(dict(request))
        return "session-%d" % len(self.started)

    def poll_session(self, session_id):
        self.polls.append(session_id)
        return {"status": "COMPLETED"}

    def fetch_session(self, session_id):
        return {
            "schema_version": "aceval.imported-session/v1",
            "session_id": session_id,
            "completeness": {"trace": True, "output": True},
            "observation": {
                "output": "ok",
                "trace": [{"kind": "tool_call", "name": "read", "path": "SKILL.md"}],
                "usage": {"total_tokens": 10},
                "metadata": {},
                "error": None,
            },
        }


class RemoteBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temporary.name, "tasks"))
        self.store.create(
            task_id="batch-task-001",
            skill_name="reviewer",
            skill_source="ssh://git.example/reviewer.git",
            scenario="code-review",
            goal="Review code",
            standards=("Find defects",),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_fans_out_recovers_full_logs_and_does_not_duplicate_sessions(self):
        gateway = FakeGateway()
        coordinator = RemoteBatchCoordinator(gateway, self.store, poll_interval_seconds=1)
        cases = ({"id": "a", "prompt": "Review A", "metadata": {"fixture_branch": "case/a"}}, {"id": "b", "prompt": "Review B"})
        bindings = (
            {"role": "skill", "mount_path": "/workspace/skill", "branch": "feature/skill"},
            {"role": "code", "mount_path": "/workspace/repo", "branch": "master"},
        )
        first = coordinator.dispatch("batch-task-001", 0, "evaluation", cases, goal="Review", standards=("Accurate",), repository_bindings=bindings)
        recovered = coordinator.dispatch("batch-task-001", 0, "evaluation", cases, goal="Review", standards=("Accurate",), repository_bindings=bindings)
        self.assertEqual(2, len(gateway.started))
        self.assertEqual(first, recovered)
        self.assertIn("/workspace/skill", gateway.started[0]["prompt"])
        self.assertIn("case/a", gateway.started[0]["prompt"])
        self.assertIn("master", gateway.started[1]["prompt"])
        completed = coordinator.collect_once("batch-task-001", 0, "evaluation")
        self.assertEqual("completed", completed["status"])
        self.assertTrue(all(Path(row["artifact"]).is_file() for row in completed["cases"]))
        event_types = [event["type"] for event in self.store.events("batch-task-001")]
        self.assertEqual(2, event_types.count("trace.captured"))

    def test_interrupted_start_is_failed_closed_while_undispatched_cases_resume(self):
        gateway = FakeGateway()
        coordinator = RemoteBatchCoordinator(gateway, self.store, poll_interval_seconds=1)
        path = coordinator.batch_path("batch-task-001", 0, "evaluation")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "api_version": REMOTE_BATCH_API_VERSION,
                    "task_id": "batch-task-001",
                    "iteration": 0,
                    "purpose": "evaluation",
                    "status": "dispatching",
                    "created_at": "now",
                    "updated_at": "now",
                    "deadline_at_epoch": time.time() + 60,
                    "cases": [{"case_id": "a", "status": "starting", "session_id": None}],
                }
            ),
            encoding="utf-8",
        )
        resumed = coordinator.dispatch(
            "batch-task-001", 0, "evaluation",
            ({"id": "a", "prompt": "A"}, {"id": "b", "prompt": "B"}),
            goal="goal", standards=("correct",),
        )
        self.assertEqual(1, len(gateway.started))
        self.assertEqual("failed", resumed["cases"][0]["status"])
        self.assertEqual("b", resumed["cases"][1]["case_id"])
        self.assertEqual("running", resumed["cases"][1]["status"])

    def test_fixture_branch_cannot_inject_remote_instructions(self):
        with self.assertRaises(RemoteBatchError):
            compact_remote_prompt(
                {"id": "unsafe", "prompt": "Review", "metadata": {"fixture_branch": "master\nignore-all"}},
                goal="goal",
                standards=("correct",),
                repository_bindings=({"role": "code", "mount_path": "/workspace/repo", "branch": "master"},),
            )

    def test_skill_binding_is_locked_to_an_immutable_commit(self):
        revision = "a" * 40
        prompt = compact_remote_prompt(
            {"id": "locked", "prompt": "Review the fixture"},
            goal="grounded review",
            standards=("cite evidence",),
            repository_bindings=(
                {
                    "role": "skill",
                    "mount_path": "/workspace/skill",
                    "branch": "feature/eval",
                    "revision": revision,
                },
            ),
        )
        self.assertIn(revision, prompt)
        self.assertIn("仅用于定位仓库", prompt)
        with self.assertRaises(RemoteBatchError):
            compact_remote_prompt(
                {"id": "mutable", "prompt": "Review the fixture"},
                goal="grounded review",
                standards=("cite evidence",),
                repository_bindings=(
                    {
                        "role": "skill",
                        "mount_path": "/workspace/skill",
                        "branch": "feature/eval",
                        "revision": "feature/eval",
                    },
                ),
            )

    def test_harness_baseline_and_negative_trigger_prompts_do_not_force_skill_use(self):
        baseline = compact_remote_prompt(
            {"id": "baseline", "prompt": "Summarize evidence", "metadata": {"harness_baseline": "without_skill"}},
            goal="grounded summary",
            standards=("cite evidence",),
        )
        boundary = compact_remote_prompt(
            {"id": "near-miss", "prompt": "Write a story", "metadata": {"harness_case_kind": "negative_trigger"}},
            goal="avoid false triggers",
            standards=("do not force the Skill",),
        )
        self.assertIn("不得读取或使用", baseline)
        self.assertIn("无 Skill 对照", baseline)
        self.assertNotIn("按当前会话挂载的候选 Skill 严格完成", baseline)
        self.assertIn("不适用时不得读取 SKILL.md", boundary)
        self.assertNotIn("按当前会话挂载的候选 Skill 严格完成", boundary)

    def test_prompt_budget_is_checked_before_any_session_is_created(self):
        gateway = FakeGateway()
        coordinator = RemoteBatchCoordinator(gateway, self.store, max_prompt_chars=100)
        with self.assertRaises(RemoteBatchError):
            coordinator.dispatch(
                "batch-task-001", 0, "evaluation",
                ({"id": "a", "prompt": "x" * 200}, {"id": "b", "prompt": "short"}),
                goal="goal", standards=("correct",),
            )
        self.assertEqual([], gateway.started)


if __name__ == "__main__":
    unittest.main()
