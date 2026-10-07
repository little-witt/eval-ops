from pathlib import Path
import tempfile
import unittest

from aceval.contracts import Artifact, GradeStatus, Oracle, RunObservation, TraceEvent
from aceval.graders import (
    ArtifactExistsGrader,
    JsonPathGrader,
    JsonSchemaGrader,
    RecordMatchGrader,
    SourceReferenceGrader,
    TraceAssertGrader,
    WorkspaceDiffGrader,
    builtin_graders,
)


def artifact(path, content):
    return Artifact(path=path, content=content, size=len(content))


class GraderTest(unittest.IsolatedAsyncioTestCase):
    async def test_json_schema_passes_common_subset_and_fails_invalid_output(self):
        grader = JsonSchemaGrader()
        params = {
            "schema": {
                "type": "object",
                "required": ["count"],
                "properties": {"count": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            }
        }
        passed = await grader.evaluate(RunObservation(output='{"count":2}'), Oracle(), params)
        failed = await grader.evaluate(RunObservation(output='{"count":0}'), Oracle(), params)
        self.assertEqual(GradeStatus.PASS, passed.status)
        self.assertEqual(GradeStatus.FAIL, failed.status)

    async def test_json_schema_rejects_non_finite_json_constants(self):
        grader = JsonSchemaGrader()
        params = {
            "schema": {
                "type": "object",
                "required": ["count"],
                "properties": {"count": {"type": "number"}},
            }
        }
        values = (
            '{"count":NaN}',
            '{"count":Infinity}',
            '{"count":-Infinity}',
            {"count": float("nan")},
        )

        for value in values:
            with self.subTest(value=value):
                result = await grader.evaluate(RunObservation(output=value), Oracle(), params)

                self.assertEqual(GradeStatus.FAIL, result.status)
                self.assertIn("not valid JSON", result.message)

    async def test_json_schema_uses_strict_json_equality(self):
        grader = JsonSchemaGrader()
        for schema in ({"enum": [1]}, {"const": 1}):
            with self.subTest(schema=schema):
                result = await grader.evaluate(
                    RunObservation(output="true"), Oracle(), {"schema": schema}
                )
                self.assertEqual(GradeStatus.FAIL, result.status)

        numeric = await grader.evaluate(
            RunObservation(output="1.0"), Oracle(), {"schema": {"const": 1}}
        )
        self.assertEqual(GradeStatus.PASS, numeric.status)

    async def test_json_schema_applies_ref_siblings(self):
        schema = {
            "$defs": {"number": {"type": "integer"}},
            "$ref": "#/$defs/number",
            "minimum": 5,
        }
        result = await JsonSchemaGrader().evaluate(
            RunObservation(output="1"), Oracle(), {"schema": schema}
        )
        self.assertEqual(GradeStatus.FAIL, result.status)

    async def test_json_schema_rejects_invalid_keyword_shapes_before_evaluation(self):
        cases = (
            ("42", {"properties": "invalid"}),
            ("42", {"required": "invalid"}),
            ("{}", {"minimum": "invalid"}),
            ("[]", {"uniqueItems": "yes"}),
        )
        for output, schema in cases:
            with self.subTest(schema=schema):
                result = await JsonSchemaGrader().evaluate(
                    RunObservation(output=output), Oracle(), {"schema": schema}
                )
                self.assertEqual(GradeStatus.ERROR, result.status)
                self.assertIn("invalid or unsupported schema", result.message)

    async def test_json_schema_loads_safe_schema_ref(self):
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            (root / "schema.json").write_text('{"type":"string"}', encoding="utf-8")
            result = await JsonSchemaGrader().evaluate(
                RunObservation(output='"ok"'),
                Oracle(),
                {"schema_ref": "schema.json", "pack_root": str(root)},
            )
            self.assertEqual(GradeStatus.PASS, result.status)

    async def test_json_schema_rejects_non_finite_constants_in_schema_ref(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as root_value:
                root = Path(root_value)
                (root / "schema.json").write_text(
                    '{"type":"number","maximum":%s}' % constant,
                    encoding="utf-8",
                )

                result = await JsonSchemaGrader().evaluate(
                    RunObservation(output="1"),
                    Oracle(),
                    {"schema_ref": "schema.json", "pack_root": str(root)},
                )

                self.assertEqual(GradeStatus.ERROR, result.status)
                self.assertIn("not valid JSON", result.message)

    async def test_json_path_uses_oracle_without_copying_gold_into_params(self):
        result = await JsonPathGrader().evaluate(
            RunObservation(output={"total": 3}),
            Oracle(data={"checks": {"total": 3}}),
            {"path": "$.total", "equals_from_oracle": "$.checks.total"},
        )
        self.assertEqual(GradeStatus.PASS, result.status)

    async def test_json_path_and_record_match_use_strict_json_equality(self):
        path_result = await JsonPathGrader().evaluate(
            RunObservation(output={"value": True}),
            Oracle(),
            {"path": "$.value", "equals": 1},
        )
        record_result = await RecordMatchGrader().evaluate(
            RunObservation(output={"records": [{"id": True}]}),
            Oracle(data={"expected_records": [{"id": 1}]}),
            {"collection_path": "$.records", "match_fields": ["id"]},
        )
        self.assertEqual(GradeStatus.FAIL, path_result.status)
        self.assertEqual(GradeStatus.FAIL, record_result.status)

    async def test_record_match_requires_expected_and_rejects_forbidden(self):
        observation = RunObservation(output={"findings": [{"id": "a"}, {"id": "bad"}]})
        oracle = Oracle(data={"expected_records": [{"id": "a"}], "forbidden_records": [{"id": "bad"}]})
        result = await RecordMatchGrader().evaluate(
            observation, oracle, {"collection_path": "$.findings", "match_fields": ["id"]}
        )
        self.assertEqual(GradeStatus.FAIL, result.status)
        self.assertEqual(1, result.metrics["forbidden_hits"])

    async def test_artifact_exists_checks_size_and_hash(self):
        value = artifact("summary.json", b"{}")
        observation = RunObservation(artifacts={"summary.json": value})
        result = await ArtifactExistsGrader().evaluate(
            observation, Oracle(), {"path": "summary.json", "min_bytes": 2}
        )
        self.assertEqual(GradeStatus.PASS, result.status)

    async def test_source_reference_uses_frozen_file_content(self):
        observation = RunObservation(
            output={"findings": [{"file": "app.py", "line": 2}]},
            pre_state={"app.py": artifact("app.py", b"one\ntwo\n")},
            post_state={"app.py": artifact("app.py", b"one\ntwo\n")},
        )
        result = await SourceReferenceGrader().evaluate(
            observation,
            Oracle(),
            {"collection_path": "$.findings", "file_field": "file", "line_field": "line"},
        )
        self.assertEqual(GradeStatus.PASS, result.status)

    async def test_trace_assert_checks_order_and_counts(self):
        observation = RunObservation(
            trace=(
                TraceEvent(kind="tool_call", name="read_file"),
                TraceEvent(kind="tool_call", name="write_file"),
            )
        )
        result = await TraceAssertGrader().evaluate(
            observation,
            Oracle(),
            {"required_tools": ["read_file"], "ordered_tools": ["read_file", "write_file"], "max_calls": 2},
        )
        self.assertEqual(GradeStatus.PASS, result.status)

    async def test_trace_assert_detects_failed_tool_result_payload(self):
        observation = RunObservation(
            trace=(
                TraceEvent(
                    kind="tool_result",
                    name="read_file",
                    payload={"ok": False, "error": "permission denied"},
                ),
            )
        )
        result = await TraceAssertGrader().evaluate(
            observation, Oracle(), {"no_errors": True}
        )

        self.assertEqual(GradeStatus.FAIL, result.status)
        self.assertEqual(1, result.metrics["failures"])

    async def test_trace_assert_normalizes_tool_event_aliases(self):
        observation = RunObservation(
            trace=(
                {"kind": None, "type": "tool_call", "name": "read_file"},
                {"kind": "", "type": "tool", "name": "write_file"},
            )
        )
        result = await TraceAssertGrader().evaluate(
            observation,
            Oracle(),
            {"required_tools": ["read_file"], "forbidden_tools": ["write_file"]},
        )

        self.assertEqual(GradeStatus.FAIL, result.status)

    async def test_workspace_diff_enforces_allowed_and_required_paths(self):
        before = {"input.csv": artifact("input.csv", b"a")}
        after = {**before, "summary.json": artifact("summary.json", b"{}")}
        result = await WorkspaceDiffGrader().evaluate(
            RunObservation(pre_state=before, post_state=after),
            Oracle(),
            {"allowed_paths": ["summary.json"], "required_paths": ["summary.json"]},
        )
        self.assertEqual(GradeStatus.PASS, result.status)

    async def test_missing_observation_is_not_evaluable(self):
        result = await JsonSchemaGrader().evaluate(RunObservation(), Oracle(), {"schema": {}})
        self.assertEqual(GradeStatus.NOT_EVALUABLE, result.status)

    def test_builtin_ids_are_stable(self):
        self.assertEqual(
            {
                "json_schema",
                "json_path",
                "record_match",
                "artifact_exists",
                "source_reference",
                "trace_assert",
                "workspace_diff",
                "code_review_findings_v1",
            },
            set(builtin_graders()),
        )


if __name__ == "__main__":
    unittest.main()
