import json
from pathlib import Path
import tempfile
import unittest

from aceval.skill_analysis import (
    CapabilityGraph,
    SkillAnalysisError,
    analyze_skill,
)
from aceval.subjects import SkillMarkdownSubjectAdapter


COMPLEX_SKILL = """---
name: report-builder
description: Build a checked report.
---
# Report Builder

## Validate input

Inputs: `input.csv`
Outputs: validation result

1. Use `read_file` to read `input.csv`.
2. Validate every row.
3. If the file is empty, return a structured error.

Never silently accept a partial parse.

## Generate report

Inputs: validation result
Outputs: `report.json`

1. Use `write_file` to create `report.json`.
2. When validation failed, do not write the report.
3. Format prose as appropriate.

## Tools

- `jq`
"""


class SkillAnalysisTest(unittest.TestCase):
    def test_extracts_source_grounded_inventory_deterministically(self):
        first = analyze_skill(COMPLEX_SKILL)
        second = analyze_skill(COMPLEX_SKILL)

        self.assertEqual(first, second)
        self.assertEqual("aceval.skill-analysis/v1", first.api_version)
        self.assertTrue(first.subject_hash.startswith("sha256:"))
        self.assertEqual(
            ["Validate input", "Generate report"],
            [item.name for item in first.capabilities],
        )
        validate, generate = first.capabilities
        self.assertEqual(("input.csv",), validate.inputs)
        self.assertEqual(("validation result",), validate.outputs)
        self.assertEqual(("read_file",), validate.tools)
        self.assertEqual(1, len(validate.branches))
        self.assertIn("file is empty", validate.branches[0].condition)
        self.assertTrue(validate.risks)
        self.assertEqual(("report.json",), generate.outputs)
        self.assertEqual(("write_file",), generate.tools)
        self.assertTrue(generate.side_effects)
        self.assertEqual(
            {"jq", "read_file", "write_file"},
            {item.name for item in first.tools},
        )
        self.assertIn(
            "subjective_or_vague",
            {item.reason_code for item in first.ambiguities},
        )
        self.assertIn(
            "unassigned_tool_dependency",
            {item.reason_code for item in first.ambiguities},
        )
        for capability in first.capabilities:
            self.assertTrue(capability.source_refs)
            self.assertTrue(
                all(ref.binding == "explicit" for ref in capability.source_refs)
            )
        first.validate_source(COMPLEX_SKILL)

    def test_round_trips_a_strict_versioned_json_contract(self):
        graph = analyze_skill(COMPLEX_SKILL)
        encoded = graph.to_json()
        loaded = CapabilityGraph.from_json(encoded)

        self.assertEqual(graph, loaded)
        self.assertEqual(graph.to_dict(), json.loads(encoded))

        invalid = graph.to_dict()
        invalid["unexpected"] = True
        with self.assertRaisesRegex(SkillAnalysisError, "unsupported fields"):
            CapabilityGraph.from_dict(invalid)

        unsupported = graph.to_dict()
        unsupported["api_version"] = "aceval.skill-analysis/v2"
        with self.assertRaisesRegex(SkillAnalysisError, "unsupported.*api_version"):
            CapabilityGraph.from_dict(unsupported)

        malformed = graph.to_dict()
        malformed["capabilities"] = "not-an-array"
        with self.assertRaisesRegex(SkillAnalysisError, "JSON array"):
            CapabilityGraph.from_dict(malformed)

        with self.assertRaisesRegex(SkillAnalysisError, "invalid.*JSON"):
            CapabilityGraph.from_json(
                '{"api_version":"aceval.skill-analysis/v1",'
                '"api_version":"aceval.skill-analysis/v1"}'
            )

    def test_path_and_frozen_snapshot_have_the_same_subject_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SKILL.md").write_text(COMPLEX_SKILL, encoding="utf-8")
            (root / "subject.json").write_text(
                '{"metadata":{"id":"report-builder","version":"1.0"}}',
                encoding="utf-8",
            )
            snapshot = SkillMarkdownSubjectAdapter().snapshot(str(root))

            from_path = analyze_skill(root)
            from_string_path = analyze_skill(str(root))
            from_snapshot = analyze_skill(snapshot)

        self.assertEqual(from_path, from_snapshot)
        self.assertEqual(from_path, from_string_path)
        self.assertEqual("sha256:%s" % snapshot.content_hash, from_path.subject_hash)
        self.assertEqual("SKILL.md", from_path.source_path)

    def test_skill_file_and_checkout_directory_share_resource_closure_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "references").mkdir()
            (root / "references" / "rules.md").write_text(
                "Use the reviewed rules.\n", encoding="utf-8"
            )
            (root / "SKILL.md").write_text(
                "# Review\n\nOutputs:\n- result\n\n"
                "See [rules](references/rules.md).\n",
                encoding="utf-8",
            )
            (root / "subject.json").write_text(
                '{"metadata":{"id":"review","version":"1"}}',
                encoding="utf-8",
            )

            directory_snapshot = SkillMarkdownSubjectAdapter().snapshot(str(root))
            file_snapshot = SkillMarkdownSubjectAdapter().snapshot(
                str(root / "SKILL.md")
            )
            directory_graph = analyze_skill(root)
            file_graph = analyze_skill(root / "SKILL.md")

        self.assertEqual(directory_snapshot.content_hash, file_snapshot.content_hash)
        self.assertEqual(directory_snapshot.files, file_snapshot.files)
        self.assertEqual(directory_graph, file_graph)

    def test_fails_closed_for_invalid_utf8_hashes_and_changed_source(self):
        with self.assertRaisesRegex(SkillAnalysisError, "UTF-8"):
            analyze_skill(b"\xff")

        graph = analyze_skill(COMPLEX_SKILL)
        with self.assertRaisesRegex(SkillAnalysisError, "quote hash mismatch"):
            graph.validate_source(COMPLEX_SKILL.replace("Validate every row", "Skip rows"))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SKILL.md").write_text(COMPLEX_SKILL, encoding="utf-8")
            snapshot = SkillMarkdownSubjectAdapter().snapshot(str(root))
            invalid = type(snapshot)(
                kind=snapshot.kind,
                uri=snapshot.uri,
                content_hash="0" * 64,
                content=snapshot.content,
                files=snapshot.files,
                metadata=snapshot.metadata,
            )
            with self.assertRaisesRegex(SkillAnalysisError, "content hash mismatch"):
                analyze_skill(invalid)

    def test_root_fallback_is_explicit_about_missing_observable_output(self):
        graph = analyze_skill(
            "# Helper\n\nInspect the request and return high quality Python code.\n"
        )

        self.assertEqual(1, len(graph.capabilities))
        self.assertFalse(graph.capabilities[0].inferred)
        self.assertEqual((), graph.tools)
        ambiguity = next(
            item
            for item in graph.ambiguities
            if item.reason_code == "missing_declared_output"
        )
        self.assertEqual(graph.capabilities[0].id, ambiguity.capability_id)
        self.assertEqual("inferred", ambiguity.source_ref.binding)

    def test_resource_closure_normalizes_nested_parent_reference_within_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "references").mkdir()
            (root / "scripts").mkdir()
            (root / "SKILL.md").write_text(
                "# Review\n\nOutputs:\n- result\n\nSee [rules](references/rules.md).\n",
                encoding="utf-8",
            )
            (root / "references" / "rules.md").write_text(
                "Run [the checker](../scripts/check.py).\n",
                encoding="utf-8",
            )
            (root / "scripts" / "check.py").write_text(
                "print('ok')\n", encoding="utf-8"
            )

            snapshot = SkillMarkdownSubjectAdapter().snapshot(str(root))

        self.assertIn("references/rules.md", snapshot.files)
        self.assertIn("scripts/check.py", snapshot.files)
        self.assertFalse(snapshot.metadata["resource_issues"])

    def test_missing_and_escaping_resource_references_are_not_silent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "references").mkdir()
            (root / "SKILL.md").write_text(
                "# Review\n\nOutputs:\n- result\n\nSee [missing](references/missing.md).\n",
                encoding="utf-8",
            )
            (root / "references" / "rules.md").write_text(
                "Do not load [outside](../../private.txt).\n",
                encoding="utf-8",
            )

            snapshot = SkillMarkdownSubjectAdapter().snapshot(str(root))

        issues = snapshot.metadata["resource_issues"]
        self.assertTrue(
            any("does not exist" in item["reason"] for item in issues), issues
        )
        self.assertTrue(
            any("escaped its root" in item["reason"] for item in issues), issues
        )


if __name__ == "__main__":
    unittest.main()
