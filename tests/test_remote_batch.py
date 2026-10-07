import json
import tempfile
import time
import unittest
from pathlib import Path

from aceval.remote_batch import RemoteBatchCoordinator, RemoteBatchError, compact_remote_prompt
from aceval.kernel_contracts import REMOTE_BATCH_API_VERSION
from aceval.catx_bindings import CATX_EXECUTION_BINDING_API_VERSION, CatxExecutionBinding
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


class RetryGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.fail_first = True

    def start_session(self, request):
        self.started.append(dict(request))
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("temporary create failure")
        return "session-%d" % len(self.started)


class AlwaysFailGateway(FakeGateway):
    def start_session(self, request):
        self.started.append(dict(request))
        raise RuntimeError("legacy session creation failure")


class BoundGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.bindings = []
        self.last_binding_evidence = {"verified": True}

    def start_session(self, request, *, binding=None):
        self.started.append(dict(request))
        self.bindings.append(binding)
        return "session-%d" % len(self.started)


class CachedLogGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.fetch_events_calls = 0
        self.events = [
            {"type": "user.message", "content": [{"type": "text", "text": "review"}]},
            {"type": "agent.message", "content": [{"type": "text", "text": "ok"}]},
        ]

    def last_event_log(self, session_id):
        encoded = json.dumps(self.events, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        import hashlib
        return {
            "events": self.events,
            "integrity": {
                "complete": True,
                "reason_codes": [],
                "page_count": 1,
                "event_count": 2,
                "event_types": ["agent.message", "user.message"],
                "required_event_types": ["user.message", "agent.message"],
                "missing_required_event_types": [],
                "sequence_status": "not_provided",
                "event_log_sha256": hashlib.sha256(encoded).hexdigest(),
            },
        }

    def fetch_events(self, session_id):
        self.fetch_events_calls += 1
        raise AssertionError("collector fetched a second, inconsistent event snapshot")


class IncompleteLogGateway(CachedLogGateway):
    def last_event_log(self, session_id):
        value = dict(super().last_event_log(session_id))
        integrity = dict(value["integrity"])
        integrity.update(
            {
                "complete": False,
                "reason_codes": ["sequence_gap"],
                "sequence_status": "gap",
            }
        )
        value["integrity"] = integrity
        return value


class MismatchedCatxSealGateway(CachedLogGateway):
    def fetch_session(self, session_id):
        value = dict(super().fetch_session(session_id))
        value["source"] = "catx_session_api"
        return value

    def last_event_log(self, session_id):
        value = dict(super().last_event_log(session_id))
        integrity = dict(value["integrity"])
        integrity["event_log_sha256"] = "0" * 64
        value["integrity"] = integrity
        return value


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
        self.assertNotIn("总体目标", baseline)
        self.assertNotIn("grounded summary", baseline)
        self.assertNotIn("按当前会话挂载的候选 Skill 严格完成", baseline)
        self.assertIn("不适用时不得读取 SKILL.md", boundary)
        self.assertNotIn("验收标准", boundary)
        self.assertNotIn("avoid false triggers", boundary)
        self.assertNotIn("按当前会话挂载的候选 Skill 严格完成", boundary)

    def test_regular_prompt_hides_control_plane_goal_and_uses_pr_commits(self):
        base = "1" * 40
        head = "2" * 40
        prompt = compact_remote_prompt(
            {
                "id": "pr-case",
                "prompt": "Review only the introduced defect",
                "metadata": {
                    "fixture_branch": "case/pr-case",
                    "base_commit": base,
                    "head_commit": head,
                },
            },
            goal="SECRET EVALUATION TARGET",
            standards=("SECRET ACCEPTANCE STANDARD",),
            repository_bindings=(
                {"role": "skill", "mount_path": "/workspace/skill", "branch": "skill/eval"},
                {"role": "code", "mount_path": "/workspace/repo", "branch": "master"},
            ),
        )
        self.assertIn("使用目标 Skill 评审下面 PR", prompt)
        self.assertIn(base, prompt)
        self.assertIn(head, prompt)
        self.assertNotIn("SECRET EVALUATION TARGET", prompt)
        self.assertNotIn("SECRET ACCEPTANCE STANDARD", prompt)
        self.assertNotIn("总体目标", prompt)
        self.assertNotIn("验收标准", prompt)

    def test_each_case_uses_its_own_immutable_pr_binding(self):
        gateway = BoundGateway()
        coordinator = RemoteBatchCoordinator(gateway, self.store)
        bindings = {
            case_id: CatxExecutionBinding(
                api_version=CATX_EXECUTION_BINDING_API_VERSION,
                subject_hash="sha256:" + "a" * 64,
                skill_ref="ssh://git.example/skill.git",
                repository_ref="ssh://git.example/fixtures.git",
                repository_hash="sha256:" + "b" * 64,
                base_commit=base,
                head_commit=head,
                metadata={"case_id": case_id},
            )
            for case_id, base, head in (
                ("a", "1" * 40, "2" * 40),
                ("b", "3" * 40, "4" * 40),
            )
        }
        batch = coordinator.dispatch(
            "batch-task-001",
            0,
            "evaluation",
            ({"id": "a", "prompt": "A"}, {"id": "b", "prompt": "B"}),
            goal="hidden",
            standards=("hidden",),
            execution_bindings=bindings,
        )
        self.assertEqual(["1" * 40, "3" * 40], [item.base_commit for item in gateway.bindings])
        self.assertEqual(["2" * 40, "4" * 40], [item.head_commit for item in gateway.bindings])
        self.assertEqual(bindings["a"].binding_hash, batch["cases"][0]["execution_binding"]["binding_hash"])

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

    def test_persisted_batch_reuse_rejects_changed_prompt_or_case_set(self):
        gateway = FakeGateway()
        coordinator = RemoteBatchCoordinator(gateway, self.store, poll_interval_seconds=1)
        coordinator.dispatch(
            "batch-task-001", 0, "evaluation",
            ({"id": "a", "prompt": "Review A"},),
            goal="goal", standards=("correct",),
        )
        with self.assertRaisesRegex(RemoteBatchError, "request does not match"):
            coordinator.dispatch(
                "batch-task-001", 0, "evaluation",
                ({"id": "a", "prompt": "Review a different fixture"},),
                goal="goal", standards=("correct",),
            )
        with self.assertRaisesRegex(RemoteBatchError, "request does not match|exactly the requested cases"):
            coordinator.dispatch(
                "batch-task-001", 0, "evaluation",
                ({"id": "a", "prompt": "Review A"}, {"id": "b", "prompt": "Review B"}),
                goal="goal", standards=("correct",),
            )

    def test_failed_session_can_be_retried_without_repeating_successful_cases(self):
        gateway = RetryGateway()
        coordinator = RemoteBatchCoordinator(gateway, self.store, poll_interval_seconds=1)
        first = coordinator.dispatch(
            "batch-task-001", 0, "evaluation",
            ({"id": "a", "prompt": "Review A"}, {"id": "b", "prompt": "Review B"}),
            goal="goal", standards=("correct",),
        )
        self.assertEqual(["failed", "running"], [row["status"] for row in first["cases"]])
        retried = coordinator.retry_failed("batch-task-001", 0, "evaluation")
        self.assertEqual(3, len(gateway.started))
        self.assertEqual("session-2", first["cases"][1]["session_id"])
        self.assertEqual("session-3", retried["cases"][0]["session_id"])
        self.assertEqual("running", retried["status"])
        retried_case = retried["cases"][0]
        self.assertEqual(2, retried_case["attempt_number"])
        self.assertEqual(1, len(retried_case["attempts"]))
        self.assertEqual("failed", retried_case["attempts"][0]["status"])
        self.assertIn("temporary create failure", retried_case["attempts"][0]["error"])
        self.assertIn(
            "temporary create failure",
            retried_case["attempts"][0]["retry_reason"],
        )

    def test_retry_refreshes_expired_batch_deadline(self):
        gateway = RetryGateway()
        coordinator = RemoteBatchCoordinator(gateway, self.store, poll_interval_seconds=1, max_wait_seconds=30)
        first = coordinator.dispatch(
            "batch-task-001", 0, "evaluation", ({"id": "a", "prompt": "Review A"},),
            goal="goal", standards=("correct",),
        )
        self.assertEqual("failed", first["cases"][0]["status"])
        path = coordinator.batch_path("batch-task-001", 0, "evaluation")
        expired = json.loads(path.read_text(encoding="utf-8"))
        expired["deadline_at_epoch"] = time.time() - 1
        path.write_text(json.dumps(expired), encoding="utf-8")

        retried = coordinator.retry_failed("batch-task-001", 0, "evaluation")
        self.assertEqual("running", retried["status"])
        self.assertGreater(float(retried["deadline_at_epoch"]), time.time())

    def test_completed_legacy_failure_batch_is_retried_when_bindings_become_available(self):
        first_gateway = AlwaysFailGateway()
        first = RemoteBatchCoordinator(first_gateway, self.store).dispatch(
            "batch-task-001", 0, "evaluation",
            ({"id": "a", "prompt": "Review A"},),
            goal="goal", standards=("correct",),
        )
        self.assertEqual("completed", first["status"])
        self.assertEqual("failed", first["cases"][0]["status"])

        binding = CatxExecutionBinding(
            api_version=CATX_EXECUTION_BINDING_API_VERSION,
            subject_hash="sha256:" + "a" * 64,
            skill_ref="ssh://git.example/skill.git",
            repository_ref="ssh://git.example/fixtures.git",
            repository_hash="sha256:" + "b" * 64,
            base_commit="1" * 40,
            head_commit="2" * 40,
            metadata={"case_id": "a"},
        )
        second_gateway = BoundGateway()
        resumed = RemoteBatchCoordinator(second_gateway, self.store).dispatch(
            "batch-task-001", 0, "evaluation",
            ({"id": "a", "prompt": "Review A with generated PR"},),
            goal="goal", standards=("correct",),
            execution_bindings={"a": binding},
        )
        self.assertEqual("running", resumed["status"])
        self.assertEqual(1, len(second_gateway.started))
        self.assertEqual(binding.base_commit, second_gateway.bindings[0].base_commit)
        self.assertEqual("running", resumed["cases"][0]["status"])

    def test_collection_reuses_the_session_snapshot_and_records_hash_receipt(self):
        gateway = CachedLogGateway()
        coordinator = RemoteBatchCoordinator(gateway, self.store, poll_interval_seconds=1)
        coordinator.dispatch(
            "batch-task-001", 0, "evaluation",
            ({"id": "a", "prompt": "Review A"},),
            goal="goal", standards=("correct",),
        )

        completed = coordinator.collect_once("batch-task-001", 0, "evaluation")
        row = completed["cases"][0]
        payload = json.loads(Path(row["artifact"]).read_text(encoding="utf-8"))

        self.assertEqual(0, gateway.fetch_events_calls)
        self.assertTrue(payload["log_completeness"]["complete"])
        self.assertEqual(64, len(payload["log_completeness"]["event_log_sha256"]))
        self.assertEqual(1, len(row["attempts"]))
        self.assertEqual(row["artifact"], row["attempts"][0]["artifact"])

    def test_incomplete_catx_log_is_retriable_infrastructure_failure(self):
        gateway = IncompleteLogGateway()
        coordinator = RemoteBatchCoordinator(gateway, self.store, poll_interval_seconds=1)
        coordinator.dispatch(
            "batch-task-001", 0, "evaluation",
            ({"id": "a", "prompt": "Review A"},),
            goal="goal", standards=("correct",),
        )

        completed = coordinator.collect_once("batch-task-001", 0, "evaluation")
        row = completed["cases"][0]
        self.assertEqual("failed", row["status"])
        self.assertIn("sequence_gap", row["error"])
        retried = coordinator.retry_failed("batch-task-001", 0, "evaluation")
        self.assertEqual("running", retried["status"])
        self.assertEqual(2, retried["cases"][0]["attempt_number"])
        self.assertIn(
            "sequence_gap",
            retried["cases"][0]["attempts"][0]["retry_reason"],
        )

    def test_catx_server_seal_is_preserved_and_mismatch_is_not_hidden_by_local_hash(self):
        gateway = MismatchedCatxSealGateway()
        coordinator = RemoteBatchCoordinator(gateway, self.store, poll_interval_seconds=1)
        coordinator.dispatch(
            "batch-task-001", 0, "evaluation",
            ({"id": "a", "prompt": "Review A"},),
            goal="goal", standards=("correct",),
        )

        completed = coordinator.collect_once("batch-task-001", 0, "evaluation")
        row = completed["cases"][0]
        payload = json.loads(Path(row["artifact"]).read_text(encoding="utf-8"))
        integrity = payload["log_completeness"]
        self.assertEqual("0" * 64, integrity["event_log_sha256"])
        self.assertNotEqual(integrity["event_log_sha256"], integrity["computed_event_log_sha256"])
        self.assertIn("event_log_hash_mismatch", integrity["reason_codes"])
        self.assertEqual("failed", row["status"])


if __name__ == "__main__":
    unittest.main()
