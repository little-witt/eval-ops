import asyncio
from pathlib import Path
import tempfile
import unittest

from aceval.contracts import Artifact, GradeStatus, Oracle, RunContext, RuntimeResult, Scenario, TraceEvent
from aceval.drivers import ArtifactWorkspaceDriver, DriverError, RepositoryWorkspaceDriver
from aceval.graders import WorkspaceDiffGrader


class DriverTest(unittest.IsolatedAsyncioTestCase):
    async def test_artifact_driver_freezes_workspace_before_cleanup(self):
        with tempfile.TemporaryDirectory() as pack_value:
            pack_root = Path(pack_value)
            (pack_root / "fixtures").mkdir()
            (pack_root / "fixtures" / "input.csv").write_text("value\n1\n", encoding="utf-8")
            scenario = Scenario(
                id="csv",
                prompt="summarize",
                fixtures=("fixtures/input.csv",),
                metadata={"artifact_paths": ["summary.json"]},
            )
            driver = ArtifactWorkspaceDriver()
            prepared = await driver.prepare(
                scenario,
                RunContext(run_id="run", metadata={"pack_root": str(pack_root)}),
            )
            self.assertIn("input.csv", prepared.baseline_files)
            (prepared.workspace / "summary.json").write_text('{"total":1}', encoding="utf-8")
            observation = await driver.collect(
                prepared,
                RuntimeResult(final_output={"total": 1}, trace=(TraceEvent(kind="message"),)),
            )
            self.assertIn("summary.json", observation.artifacts)
            self.assertIn("summary.json", observation.post_state)
            await driver.cleanup(prepared)
            await driver.cleanup(prepared)
            self.assertFalse(prepared.workspace.exists())
            self.assertEqual(b'{"total":1}', observation.artifacts["summary.json"].content)

    async def test_repository_driver_rejects_fixture_traversal(self):
        with tempfile.TemporaryDirectory() as pack_value:
            driver = RepositoryWorkspaceDriver()
            scenario = Scenario(id="bad", fixtures=("../secret",))
            with self.assertRaises(DriverError):
                await driver.prepare(
                    scenario,
                    RunContext(run_id="run", metadata={"pack_root": pack_value}),
                )

    async def test_collect_rejects_workspace_symlinks(self):
        with tempfile.TemporaryDirectory() as pack_value:
            driver = RepositoryWorkspaceDriver()
            prepared = await driver.prepare(Scenario(id="repo"), RunContext(run_id="run"))
            try:
                (prepared.workspace / "link").symlink_to(Path(pack_value))
                with self.assertRaises(DriverError):
                    await driver.collect(prepared, RuntimeResult())
            finally:
                await driver.cleanup(prepared)

    async def test_repository_diff_observes_git_metadata_changes(self):
        driver = RepositoryWorkspaceDriver()
        prepared = await driver.prepare(Scenario(id="repo"), RunContext(run_id="run"))
        try:
            git_directory = prepared.workspace / ".git"
            git_directory.mkdir()
            (git_directory / "config").write_text(
                "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8"
            )

            observation = await driver.collect(prepared, RuntimeResult())
            grade = await WorkspaceDiffGrader().evaluate(
                observation,
                Oracle(),
                {"forbidden_paths": [".git/**"]},
            )

            self.assertIn(".git/config", observation.post_state)
            self.assertEqual(GradeStatus.FAIL, grade.status)
            self.assertIn(".git/config", grade.metrics["created"])
        finally:
            await driver.cleanup(prepared)

    async def test_contentless_runtime_artifact_requires_workspace_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = ArtifactWorkspaceDriver()
            scenario = Scenario(id="ghost-artifact", prompt="produce it")
            prepared = await driver.prepare(
                scenario,
                RunContext(run_id="ghost", metadata={"work_root": str(root)}),
            )
            forged = Artifact(
                path="ghost.json",
                content_hash="0" * 64,
                mime_type="application/json",
                size=2,
                content=None,
            )

            with self.assertRaises(DriverError):
                await driver.collect(
                    prepared, RuntimeResult(artifacts={"ghost.json": forged})
                )

            await driver.cleanup(prepared)

    async def test_runtime_artifact_objects_honor_per_file_byte_limit(self):
        driver = ArtifactWorkspaceDriver(
            max_files=10,
            max_file_bytes=3,
            max_total_bytes=100,
        )
        prepared = await driver.prepare(
            Scenario(id="large-runtime-artifact"), RunContext(run_id="run")
        )
        try:
            artifact = Artifact(path="large.bin", content=b"1234")
            with self.assertRaisesRegex(DriverError, "per-file byte limit"):
                await driver.collect(
                    prepared,
                    RuntimeResult(artifacts={"large.bin": artifact}),
                )
        finally:
            await driver.cleanup(prepared)

    async def test_runtime_artifacts_honor_file_count_limit(self):
        driver = ArtifactWorkspaceDriver(
            max_files=1,
            max_file_bytes=100,
            max_total_bytes=100,
        )
        prepared = await driver.prepare(
            Scenario(id="many-runtime-artifacts"), RunContext(run_id="run")
        )
        try:
            with self.assertRaisesRegex(DriverError, "file-count limit"):
                await driver.collect(
                    prepared,
                    RuntimeResult(
                        artifacts={"first.txt": "1", "second.txt": "2"}
                    ),
                )
        finally:
            await driver.cleanup(prepared)

    async def test_runtime_artifacts_honor_total_byte_limit(self):
        driver = ArtifactWorkspaceDriver(
            max_files=10,
            max_file_bytes=100,
            max_total_bytes=3,
        )
        prepared = await driver.prepare(
            Scenario(id="large-runtime-artifacts"), RunContext(run_id="run")
        )
        try:
            with self.assertRaisesRegex(DriverError, "total byte limit"):
                await driver.collect(
                    prepared,
                    RuntimeResult(
                        artifacts={"first.txt": "12", "second.txt": "34"}
                    ),
                )
        finally:
            await driver.cleanup(prepared)


if __name__ == "__main__":
    unittest.main()
