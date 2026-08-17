import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from aceval.agent_runtime import (
    ModelReply,
    ReferenceAgentRuntime,
    ReferenceRunResult,
    ReferenceRuntimeConfig,
    ScriptedModelClient,
    ToolCall,
)
from aceval.contracts import (
    GradeStatus,
    PreparedScenario,
    RunBudget,
    RunContext,
    RunObservation,
    RuntimeProfile,
    SubjectSnapshot,
)
from aceval.graders import TraceAssertGrader
from aceval.runtime import FakeRuntime, ReferenceRuntimeAdapter, SubprocessRuntime


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_fake_runtime_selects_candidate_and_writes_artifact(self):
        with tempfile.TemporaryDirectory() as workspace:
            prepared = PreparedScenario(
                workspace=Path(workspace),
                metadata={
                    "scenario_id": "case-1",
                    "fake_runtime": {
                        "default": {"final_output": {"ok": False}},
                        "variants": {
                            "candidate": {
                                "final_output": {"ok": True},
                                "artifacts": {"result.json": {"ok": True}},
                            }
                        },
                    },
                },
            )
            subject = SubjectSnapshot(
                kind="skill",
                uri="candidate",
                content_hash="candidate",
                metadata={"variant": "candidate"},
            )
            result = await FakeRuntime().execute(prepared, subject, RunContext(run_id="run"))
            self.assertEqual({"ok": True}, result.final_output)
            self.assertEqual({"ok": True}, json.loads((Path(workspace) / "result.json").read_text()))

    async def test_fake_runtime_uses_top_level_default_for_unknown_variant(self):
        prepared = PreparedScenario(
            metadata={"fake_runtime": {"default": {"final_output": "baseline"}, "variants": {}}}
        )
        subject = SubjectSnapshot(kind="skill", uri="skill", content_hash="hash")
        result = await FakeRuntime().execute(prepared, subject, RunContext(run_id="run"))
        self.assertEqual("baseline", result.final_output)

    async def test_fake_runtime_preserves_canonical_trace_fields_for_graders(self):
        prepared = PreparedScenario(
            metadata={
                "fake_runtime": {
                    "trace": [
                        {
                            "kind": "tool_call",
                            "name": "read",
                            "seq": 7,
                            "timestamp": 123.5,
                            "tool": "read_file",
                            "duration_ms": 12.25,
                            "error": "read failed",
                            "payload": {"path": "input.txt"},
                            "extension": "kept",
                        }
                    ]
                }
            }
        )
        subject = SubjectSnapshot(kind="skill", uri="skill", content_hash="hash")
        result = await FakeRuntime().execute(prepared, subject, RunContext(run_id="run"))

        event = result.trace[0]
        self.assertEqual(7, event.seq)
        self.assertEqual("123.5", event.timestamp)
        self.assertEqual("read_file", event.tool)
        self.assertEqual(12.25, event.duration_ms)
        self.assertEqual("read failed", event.error)
        self.assertEqual({"path": "input.txt", "extension": "kept"}, event.payload)
        self.assertTrue(
            {"seq", "timestamp", "tool", "duration_ms", "error"}.isdisjoint(event.payload)
        )

        grade = await TraceAssertGrader().evaluate(
            RunObservation(trace=result.trace),
            oracle=None,
            params={"required_tools": ["read_file"], "no_errors": True},
        )
        self.assertEqual(GradeStatus.FAIL, grade.status)
        self.assertEqual(1, grade.metrics["tool_counts"]["read_file"])

    async def test_reference_agent_adapter_converts_trace(self):
        with tempfile.TemporaryDirectory() as root_value, tempfile.TemporaryDirectory() as workspace_value:
            root = Path(root_value)
            (root / "SKILL.md").write_text("Return JSON.", encoding="utf-8")
            engine = ReferenceAgentRuntime(ScriptedModelClient([ModelReply(content='{"ok":true}')]))
            adapter = ReferenceRuntimeAdapter(engine)
            prepared = PreparedScenario(workspace=Path(workspace_value), prompt="do it")
            subject = SubjectSnapshot(kind="skill", uri=str(root), content_hash="hash")
            result = await adapter.execute(prepared, subject, RunContext(run_id="run"))
            self.assertIsNone(result.error)
            self.assertEqual('{"ok":true}', result.final_output)
            self.assertTrue(any(event.kind == "message" for event in result.trace))
            self.assertTrue(adapter.capabilities.supports("skill_activation"))

    async def test_reference_agent_adapter_enforces_remaining_tool_budget(self):
        with tempfile.TemporaryDirectory() as root_value, tempfile.TemporaryDirectory() as workspace_value:
            root = Path(root_value)
            workspace = Path(workspace_value)
            (root / "SKILL.md").write_text("Write files.", encoding="utf-8")
            engine = ReferenceAgentRuntime(
                ScriptedModelClient(
                    [
                        ModelReply(
                            tool_calls=(
                                ToolCall(
                                    "write_file",
                                    {"path": "first.txt", "content": "first"},
                                ),
                                ToolCall(
                                    "write_file",
                                    {"path": "second.txt", "content": "second"},
                                ),
                            )
                        )
                    ]
                )
            )
            adapter = ReferenceRuntimeAdapter(engine)
            context = RunContext(
                run_id="tool-budget",
                budget=RunBudget(max_tool_calls=1),
            )

            result = await adapter.execute(
                PreparedScenario(workspace=workspace, prompt="do it"),
                SubjectSnapshot(kind="skill", uri=str(root), content_hash="hash"),
                context,
            )

            self.assertIn("max_tool_calls", result.error)
            self.assertFalse((workspace / "first.txt").exists())
            self.assertFalse((workspace / "second.txt").exists())

    async def test_reference_agent_adapter_preserves_usage_on_failure(self):
        with tempfile.TemporaryDirectory() as root_value, tempfile.TemporaryDirectory() as workspace_value:
            root = Path(root_value)
            (root / "SKILL.md").write_text("Keep working.", encoding="utf-8")
            engine = ReferenceAgentRuntime(
                ScriptedModelClient(
                    [
                        ModelReply(
                            tool_calls=(ToolCall("list_files", {}),),
                            usage={"total_tokens": 5, "cost_usd": 0.02},
                        )
                    ]
                ),
                config=ReferenceRuntimeConfig(max_steps=1),
            )
            result = await ReferenceRuntimeAdapter(engine).execute(
                PreparedScenario(workspace=Path(workspace_value), prompt="do it"),
                SubjectSnapshot(kind="skill", uri=str(root), content_hash="hash"),
                RunContext(run_id="failed-usage"),
            )

            self.assertIn("max_steps", result.error)
            self.assertEqual(5, result.usage["total_tokens"])
            self.assertAlmostEqual(0.02, result.usage["cost_usd"])
            self.assertTrue(result.trace)

    async def test_reference_agent_timeout_waits_for_workspace_access_to_finish(self):
        class DelayedEngine:
            def run(self, skill_path, prompt, workspace):
                del skill_path, prompt
                time.sleep(0.08)
                (workspace / "finished.txt").write_text("done", encoding="utf-8")
                raise RuntimeError("ignored after the soft deadline")

        with tempfile.TemporaryDirectory() as root_value, tempfile.TemporaryDirectory() as workspace_value:
            root = Path(root_value)
            workspace = Path(workspace_value)
            (root / "SKILL.md").write_text("Do the work.", encoding="utf-8")
            adapter = ReferenceRuntimeAdapter(DelayedEngine())
            context = RunContext(
                run_id="timeout",
                budget=RunBudget(max_wall_time_seconds=0.01),
            )

            started = time.monotonic()
            result = await adapter.execute(
                PreparedScenario(workspace=workspace, prompt="do it"),
                SubjectSnapshot(kind="skill", uri=str(root), content_hash="hash"),
                context,
            )

            self.assertEqual("timeout", result.error)
            self.assertGreaterEqual(time.monotonic() - started, 0.07)
            self.assertEqual("done", (workspace / "finished.txt").read_text(encoding="utf-8"))

    async def test_reference_timeout_preserves_late_success_usage(self):
        class DelayedSuccessEngine:
            def run(self, skill_path, prompt, workspace):
                del skill_path, prompt, workspace
                time.sleep(0.05)
                return ReferenceRunResult(
                    final_output="late",
                    trace=({"kind": "message", "name": "late"},),
                    usage={"total_tokens": 9, "cost_usd": 0.04},
                    duration_seconds=0.05,
                )

        with tempfile.TemporaryDirectory() as root_value, tempfile.TemporaryDirectory() as workspace_value:
            root = Path(root_value)
            (root / "SKILL.md").write_text("Do the work.", encoding="utf-8")
            result = await ReferenceRuntimeAdapter(DelayedSuccessEngine()).execute(
                PreparedScenario(workspace=Path(workspace_value), prompt="do it"),
                SubjectSnapshot(kind="skill", uri=str(root), content_hash="hash"),
                RunContext(
                    run_id="late-success",
                    budget=RunBudget(max_wall_time_seconds=0.01),
                ),
            )

            self.assertEqual("timeout", result.error)
            self.assertEqual(9, result.usage["total_tokens"])
            self.assertAlmostEqual(0.04, result.usage["cost_usd"])
            self.assertTrue(result.trace)

    async def test_subprocess_runtime_is_argv_only_and_obeys_profile(self):
        with tempfile.TemporaryDirectory() as workspace:
            runtime = SubprocessRuntime(allowed_commands=(sys.executable,))
            profile = RuntimeProfile(
                adapter="subprocess",
                parameters={
                    "command": [
                        sys.executable,
                        "-c",
                        "import json,os,sys; print(json.dumps({'prompt': sys.stdin.read(), 'subject': os.environ.get('ACEVAL_SUBJECT_PATH')}))",
                    ],
                    "parse_json": True,
                },
            )
            context = RunContext(
                run_id="subprocess",
                runtime_profile=profile,
                budget=RunBudget(max_wall_time_seconds=5),
            )
            result = await runtime.execute(
                PreparedScenario(workspace=Path(workspace), prompt="hello"),
                SubjectSnapshot(kind="skill", uri="unused", content_hash="hash"),
                context,
            )
            self.assertIsNone(result.error)
            self.assertEqual("hello", result.final_output["prompt"])
            self.assertEqual("unused", result.final_output["subject"])
            self.assertTrue(runtime.capabilities.supports("skill_activation"))

    async def test_subprocess_runtime_reports_timeout(self):
        with tempfile.TemporaryDirectory() as workspace:
            runtime = SubprocessRuntime(allowed_commands=(sys.executable,))
            profile = RuntimeProfile(
                adapter="subprocess",
                parameters={"command": [sys.executable, "-c", "import time; time.sleep(2)"]},
            )
            context = RunContext(
                run_id="timeout",
                runtime_profile=profile,
                budget=RunBudget(max_wall_time_seconds=0.05),
            )
            result = await runtime.execute(
                PreparedScenario(workspace=Path(workspace)),
                SubjectSnapshot(kind="skill", uri="unused", content_hash="hash"),
                context,
            )
            self.assertEqual("timeout", result.error)

    async def test_subprocess_runtime_ignores_declarative_answers(self):
        with tempfile.TemporaryDirectory() as workspace:
            runtime = SubprocessRuntime(allowed_commands=(sys.executable,))
            profile = RuntimeProfile(
                adapter="subprocess",
                parameters={
                    "command": [sys.executable, "-c", "print('actual process output')"],
                },
            )
            prepared = PreparedScenario(
                workspace=Path(workspace),
                metadata={
                    "fake_runtime": {
                        "final_output": "fake answer",
                        "artifacts": {"fake.txt": "must not be written"},
                    },
                    "reference_runtime": {"final_output": "reference answer"},
                },
            )
            result = await runtime.execute(
                prepared,
                SubjectSnapshot(kind="skill", uri="unused", content_hash="hash"),
                RunContext(run_id="subprocess", runtime_profile=profile),
            )

            self.assertEqual("actual process output\n", result.final_output)
            self.assertFalse((Path(workspace) / "fake.txt").exists())


if __name__ == "__main__":
    unittest.main()
