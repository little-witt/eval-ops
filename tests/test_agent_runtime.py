import json
import sys
import tempfile
import unittest
from pathlib import Path

from aceval.agent_runtime import (
    CommandModelClient,
    ModelReply,
    ReferenceAgentRuntime,
    ReferenceRuntimeConfig,
    ReferenceRuntimeError,
    ScriptedModelClient,
    ToolCall,
)


class CommandModelClientTest(unittest.TestCase):
    @staticmethod
    def _client(output: str, **kwargs: object) -> CommandModelClient:
        command = (sys.executable, "-c", "import sys;sys.stdout.write(%r)" % output)
        return CommandModelClient(command, **kwargs)

    def test_accepts_strict_bridge_response(self) -> None:
        payload = {
            "content": "working",
            "tool_calls": [
                {"id": "call-1", "name": "read_file", "arguments": {"path": "x"}}
            ],
            "usage": {"input_tokens": 2, "cost_usd": 0.01},
        }

        reply = self._client(json.dumps(payload)).complete((), ())

        self.assertEqual("working", reply.content)
        self.assertEqual("call-1", reply.tool_calls[0].call_id)
        self.assertEqual({"path": "x"}, reply.tool_calls[0].arguments)
        self.assertEqual(payload["usage"], reply.usage)

    def test_rejects_malformed_bridge_response_fields(self) -> None:
        valid = {"content": "", "tool_calls": [], "usage": {}}
        malformed = (
            [],
            {"tool_calls": [], "usage": {}},
            {**valid, "content": 1},
            {**valid, "tool_calls": {}},
            {**valid, "usage": []},
            {**valid, "tool_calls": ["call"]},
            {
                **valid,
                "tool_calls": [{"id": "1", "name": "", "arguments": {}}],
            },
            {
                **valid,
                "tool_calls": [{"id": "1", "name": "read_file", "arguments": []}],
            },
            {
                **valid,
                "tool_calls": [{"id": 1, "name": "read_file", "arguments": {}}],
            },
            {
                **valid,
                "tool_calls": [{"id": "", "name": "read_file", "arguments": {}}],
            },
            {
                **valid,
                "tool_calls": [{"name": "read_file", "arguments": {}}],
            },
            {**valid, "usage": {"total_tokens": True}},
            {**valid, "usage": {"total_tokens": -1}},
            {**valid, "usage": {"total_tokens": 0.9}},
            {**valid, "usage": {"total_tokens": "1"}},
            {**valid, "usage": {"total_tokens": float("nan")}},
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    ReferenceRuntimeError, "invalid model bridge response"
                ):
                    self._client(json.dumps(payload)).complete((), ())

    def test_rejects_duplicate_tool_call_ids(self) -> None:
        payload = {
            "content": "",
            "tool_calls": [
                {"id": "same", "name": "list_files", "arguments": {}},
                {"id": "same", "name": "list_files", "arguments": {}},
            ],
            "usage": {},
        }

        with self.assertRaisesRegex(ReferenceRuntimeError, "invalid model bridge response"):
            self._client(json.dumps(payload)).complete((), ())

    def test_enforces_request_stdout_and_stderr_limits(self) -> None:
        valid_response = json.dumps({"content": "ok", "tool_calls": [], "usage": {}})
        with self.subTest(stream="request"):
            with self.assertRaisesRegex(ReferenceRuntimeError, "request exceeds byte limit"):
                self._client(valid_response, max_request_bytes=1).complete((), ())
        with self.subTest(stream="stdout"):
            with self.assertRaisesRegex(ReferenceRuntimeError, "stdout exceeds byte limit"):
                self._client(valid_response, max_stdout_bytes=8).complete((), ())
        with self.subTest(stream="stderr"):
            command = (
                sys.executable,
                "-c",
                "import sys;sys.stderr.write('x'*9);sys.stdout.write(%r)"
                % valid_response,
            )
            client = CommandModelClient(command, max_stderr_bytes=8)
            with self.assertRaisesRegex(ReferenceRuntimeError, "stderr exceeds byte limit"):
                client.complete((), ())

    def test_bridge_failure_includes_bounded_stderr(self) -> None:
        command = (
            sys.executable,
            "-c",
            "import sys;sys.stderr.write('sensitive-output');sys.exit(7)",
        )

        with self.assertRaises(ReferenceRuntimeError) as raised:
            CommandModelClient(command).complete((), ())

        self.assertEqual(
            "model bridge exited with status 7: sensitive-output",
            str(raised.exception),
        )

    def test_normalizes_utf8_encoding_and_decoding_errors(self) -> None:
        invalid_stdout = (
            sys.executable,
            "-c",
            "import sys;sys.stdout.buffer.write(bytes([255]))",
        )
        with self.assertRaisesRegex(ReferenceRuntimeError, "invalid model bridge response"):
            CommandModelClient(invalid_stdout).complete((), ())

        client = CommandModelClient((sys.executable, "-c", ""))
        with self.assertRaisesRegex(ReferenceRuntimeError, "invalid model bridge request"):
            client.complete(({"role": "user", "content": "\ud800"},), ())

    def test_requires_positive_resource_configuration(self) -> None:
        command = (sys.executable, "-c", "")
        for name in (
            "max_request_bytes",
            "max_stdout_bytes",
            "max_stderr_bytes",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    CommandModelClient(command, **{name: 0})
        with self.assertRaises(ValueError):
            CommandModelClient(command, timeout_seconds=0)
        with self.assertRaises(ValueError):
            CommandModelClient(command, max_stdout_bytes=True)

    def test_profile_hashes_argv_without_persisting_arguments(self) -> None:
        secret = "token-that-must-not-be-reported"
        client = CommandModelClient(
            (sys.executable, "--api-key", secret),
            model_id="example-model",
        )

        profile = dict(client.profile)
        encoded = json.dumps(profile, sort_keys=True)

        self.assertEqual("example-model", profile["model_id"])
        self.assertIn("argv_sha256", profile)
        self.assertNotIn("argv", profile)
        self.assertNotIn(secret, encoded)


class ReferenceAgentRuntimeTest(unittest.TestCase):
    def test_reads_fixture_and_returns_final_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "subject"
            workspace = root / "workspace"
            skill.mkdir()
            workspace.mkdir()
            (skill / "SKILL.md").write_text("Read input.txt and summarize it.", encoding="utf-8")
            (workspace / "input.txt").write_text("revenue=42", encoding="utf-8")
            client = ScriptedModelClient(
                [
                    ModelReply(tool_calls=(ToolCall("read_file", {"path": "input.txt"}),)),
                    ModelReply(content=json.dumps({"revenue": 42})),
                ]
            )

            result = ReferenceAgentRuntime(client).run(skill, "Summarize the input.", workspace)

            self.assertEqual({"revenue": 42}, json.loads(result.final_output))
            self.assertEqual(["model_call", "tool_call", "tool_result", "model_call", "message"], [event["type"] for event in result.trace])

    def test_write_tool_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            workspace = root / "workspace"
            skill.write_text("Write the requested output.", encoding="utf-8")
            client = ScriptedModelClient(
                [
                    ModelReply(
                        tool_calls=(ToolCall("write_file", {"path": "../escaped.txt", "content": "bad"}),)
                    ),
                    ModelReply(content="stopped"),
                ]
            )

            result = ReferenceAgentRuntime(client).run(skill, "Write output.", workspace)

            self.assertFalse((root / "escaped.txt").exists())
            tool_result = next(event for event in result.trace if event["type"] == "tool_result")
            self.assertFalse(tool_result["payload"]["ok"])

    def test_stops_at_step_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("Keep reading.", encoding="utf-8")
            client = ScriptedModelClient(
                [ModelReply(tool_calls=(ToolCall("list_files", {}),)) for _ in range(2)]
            )
            runtime = ReferenceAgentRuntime(client, ReferenceRuntimeConfig(max_steps=2))

            with self.assertRaises(ReferenceRuntimeError):
                runtime.run(skill, "Run.", root / "workspace")

    def test_failure_preserves_partial_usage_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("Keep reading.", encoding="utf-8")
            client = ScriptedModelClient(
                [
                    ModelReply(
                        tool_calls=(
                            ToolCall("list_files", {}, call_id="first"),
                        ),
                        usage={"total_tokens": 3, "cost_usd": 0.01},
                    ),
                    ModelReply(
                        tool_calls=(
                            ToolCall("list_files", {}, call_id="second"),
                        ),
                        usage={"total_tokens": 4, "cost_usd": 0.02},
                    ),
                ]
            )
            runtime = ReferenceAgentRuntime(
                client, ReferenceRuntimeConfig(max_steps=2)
            )

            with self.assertRaises(ReferenceRuntimeError) as raised:
                runtime.run(skill, "Run.", root / "workspace")

            self.assertEqual(7, raised.exception.usage["total_tokens"])
            self.assertAlmostEqual(0.03, raised.exception.usage["cost_usd"])
            self.assertTrue(raised.exception.trace)
            self.assertGreaterEqual(raised.exception.duration_seconds, 0)

    def test_stops_at_cumulative_tool_call_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("List files twice.", encoding="utf-8")
            client = ScriptedModelClient(
                [
                    ModelReply(
                        tool_calls=(ToolCall("list_files", {}, call_id="first"),)
                    ),
                    ModelReply(
                        tool_calls=(ToolCall("list_files", {}, call_id="second"),)
                    ),
                ]
            )
            runtime = ReferenceAgentRuntime(
                client,
                ReferenceRuntimeConfig(max_steps=2, max_tool_calls=1),
            )

            with self.assertRaisesRegex(ReferenceRuntimeError, "max_tool_calls"):
                runtime.run(skill, "Run.", root / "workspace")

    def test_rejects_oversized_tool_call_batch_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            workspace = root / "workspace"
            skill.write_text("Write two files.", encoding="utf-8")
            client = ScriptedModelClient(
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
            runtime = ReferenceAgentRuntime(
                client,
                ReferenceRuntimeConfig(max_tool_calls=1),
            )

            with self.assertRaisesRegex(ReferenceRuntimeError, "max_tool_calls"):
                runtime.run(skill, "Run.", workspace)

            self.assertFalse((workspace / "first.txt").exists())
            self.assertFalse((workspace / "second.txt").exists())

    def test_rejects_duplicate_tool_call_id_across_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("List files twice.", encoding="utf-8")
            client = ScriptedModelClient(
                [
                    ModelReply(
                        tool_calls=(ToolCall("list_files", {}, call_id="duplicate"),)
                    ),
                    ModelReply(
                        tool_calls=(ToolCall("list_files", {}, call_id="duplicate"),)
                    ),
                ]
            )

            with self.assertRaisesRegex(ReferenceRuntimeError, "duplicate tool call id"):
                ReferenceAgentRuntime(client).run(skill, "Run.", root / "workspace")

    def test_rejects_invalid_reply_from_non_command_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("Return output.", encoding="utf-8")
            client = ScriptedModelClient([ModelReply(content="ok", usage={"tokens": True})])

            with self.assertRaisesRegex(ReferenceRuntimeError, "invalid reply"):
                ReferenceAgentRuntime(client).run(skill, "Run.", root / "workspace")

    def test_requires_positive_runtime_configuration(self) -> None:
        for name in (
            "max_steps",
            "max_tool_calls",
            "max_read_bytes",
            "max_write_bytes",
            "max_trace_events",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    ReferenceRuntimeConfig(**{name: 0})
        with self.assertRaises(ValueError):
            ReferenceRuntimeConfig(max_steps=True)


if __name__ == "__main__":
    unittest.main()
