import json
import tempfile
import unittest
from pathlib import Path

from aceval.planning_workflow import (
    REFERENCE_RUNTIME_CAPABILITIES,
    create_planning_artifacts,
    load_planning_artifacts,
)


class PlanningWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text(
            """# Summary Skill

## Capabilities

### Create summary

Inputs:
- `input.csv`

Outputs:
- `summary.json`

Steps:
1. Use `read_file` to read `input.csv`.
2. Use `write_file` to create `summary.json`.

If the input is empty, return an explicit empty summary.
""",
            encoding="utf-8",
        )
        self.cases = self.root / "cases.json"
        self.cases.write_text(
            json.dumps(
                {
                    "name": "summary-planned",
                    "cases": [
                        {
                            "id": "summary-dev",
                            "prompt": "Create the summary from input.csv.",
                            "fixtures": [
                                {"path": "input.csv", "content": "value\n1\n"}
                            ],
                            "expected_output": {"count": 1},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_creates_and_reloads_versioned_planning_artifacts(self):
        output = self.root / "plan"
        artifacts = create_planning_artifacts(
            self.skill,
            self.cases,
            "Keep results correct and cover empty input.",
            output,
            runtime_capabilities=tuple(sorted(REFERENCE_RUNTIME_CAPABILITIES)),
            max_generated_cases=4,
        )

        self.assertTrue((output / "capability-graph.json").is_file())
        self.assertTrue((output / "test-plan.json").is_file())
        self.assertTrue((output / "case-drafts.json").is_file())
        self.assertTrue((output / "coverage.json").is_file())
        self.assertGreaterEqual(len(artifacts.capability_graph.capabilities), 1)
        self.assertGreaterEqual(len(artifacts.test_plan.requirements), 1)
        self.assertIn("summary-dev", artifacts.case_generation.active_case_ids)
        self.assertEqual(self.cases.parent.resolve(), artifacts.seed_source_root)

        loaded = load_planning_artifacts(output)
        self.assertEqual(
            artifacts.capability_graph.to_dict(),
            loaded.capability_graph.to_dict(),
        )
        self.assertEqual(artifacts.test_plan.to_dict(), loaded.test_plan.to_dict())
        self.assertEqual(artifacts.coverage.to_dict(), loaded.coverage.to_dict())

    def test_unknown_runtime_reports_gaps_without_claiming_execution(self):
        artifacts = create_planning_artifacts(
            self.skill,
            self.cases,
            "Cover the workflow.",
            self.root / "unknown-plan",
            runtime_capabilities=None,
            max_generated_cases=2,
        )

        self.assertEqual("unknown", artifacts.test_plan.runtime_profile_state)
        self.assertEqual(0, artifacts.coverage.executable.covered)
        self.assertTrue(artifacts.test_plan.runtime_gaps)


if __name__ == "__main__":
    unittest.main()
