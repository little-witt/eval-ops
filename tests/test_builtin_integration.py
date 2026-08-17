from pathlib import Path
import unittest

from aceval.contracts import GradeStatus, RunContext
from aceval.drivers import ArtifactWorkspaceDriver, RepositoryWorkspaceDriver
from aceval.graders import builtin_graders
from aceval.pack import EvalPackLoader
from aceval.runtime import FakeRuntime
from aceval.subjects import SkillMarkdownSubjectAdapter


ROOT = Path(__file__).resolve().parents[1]


class BuiltinIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def _run_first_case(self, pack_name, subject_name, driver):
        pack = EvalPackLoader().load(ROOT / "evalpacks" / pack_name)
        scenario = pack.scenarios[0]
        subject = SkillMarkdownSubjectAdapter().snapshot(
            str(ROOT / "examples" / "subjects" / subject_name)
        )
        context = RunContext(run_id="integration", metadata={"pack_root": str(pack.root)})
        prepared = await driver.prepare(scenario, context)
        try:
            runtime_result = await FakeRuntime().execute(prepared, subject, context)
            observation = await driver.collect(prepared, runtime_result)
        finally:
            await driver.cleanup(prepared)

        graders = builtin_graders()
        results = []
        for grader_id in scenario.grader_ids:
            spec = pack.manifest.grader_by_id(grader_id)
            params = dict(spec.params)
            params.update(scenario.scenario.grader_params.get(grader_id, {}))
            params.update({"hard": spec.hard, "pack_root": str(pack.root)})
            results.append(
                await graders[spec.type].evaluate(observation, scenario.oracle, params)
            )
        return results

    async def test_csv_candidate_passes_all_deterministic_graders(self):
        results = await self._run_first_case(
            "csv-summary-smoke", "csv-summary-skill-candidate", ArtifactWorkspaceDriver()
        )
        self.assertTrue(results)
        self.assertEqual([], [result for result in results if result.status != GradeStatus.PASS])

    async def test_security_candidate_passes_all_deterministic_graders(self):
        results = await self._run_first_case(
            "security-review", "security-review-skill-candidate", RepositoryWorkspaceDriver()
        )
        self.assertTrue(results)
        self.assertEqual([], [result for result in results if result.status != GradeStatus.PASS])


if __name__ == "__main__":
    unittest.main()
