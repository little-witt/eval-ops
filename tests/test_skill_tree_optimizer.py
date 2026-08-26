import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from aceval.agent_runtime import ModelReply, ScriptedModelClient
from aceval.git_publisher import GitSkillPublisher
from aceval.kernel_contracts import RepositoryConfig
from aceval.optimizer import CandidateRejected, FailureEvidence
from aceval.skill_tree_optimizer import SkillTreeOptimizer, SkillTreePatchPolicy, editable_skill_inventory, materialize_skill_tree_candidate, scan_skill_tree


class NoNetworkPublisher(GitSkillPublisher):
    def _git(self, root, *args, timeout=120):
        if args and args[0] == "push":
            self.pushed = tuple(args)
            return ""
        return super()._git(root, *args, timeout=timeout)


def make_alert_skill(root: Path) -> None:
    (root / "scripts" / "collectors").mkdir(parents=True)
    (root / "workflow" / "fast").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "SKILL.md").write_text(
        "# Alert level\n\nRun `scripts/collect.py`, then follow `workflow/fast/stage-3-grade.md`.\n",
        encoding="utf-8",
    )
    (root / "scripts" / "collectors" / "group_b_js.py").write_text(
        "def limit_names(names):\n    return names[:5]\n",
        encoding="utf-8",
    )
    (root / "workflow" / "fast" / "stage-3-grade.md").write_text(
        "# Grade\n\nTreat an empty trend as low risk.\n",
        encoding="utf-8",
    )
    (root / "config" / "fast-flow-whitelist.json").write_text(
        '{"enabled": true}\n', encoding="utf-8"
    )
    (root / "skill.sig").write_text("generated-signature\n", encoding="utf-8")


class SkillTreeOptimizerTests(unittest.TestCase):
    def test_materializes_two_resource_edits_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            make_alert_skill(skill)
            response = {
                "changes": [
                    {
                        "path": "scripts/collectors/group_b_js.py",
                        "operation": "replace_text",
                        "old_text": "return names[:5]",
                        "new_text": "return names[:20]",
                        "reason": "preserve all relevant new anomalies",
                    },
                    {
                        "path": "workflow/fast/stage-3-grade.md",
                        "operation": "replace_text",
                        "old_text": "Treat an empty trend as low risk.",
                        "new_text": "Treat an empty trend as not evaluable and request evidence.",
                        "reason": "absence of evidence is not low risk evidence",
                    },
                ],
                "rationale": "Repair collection and grading together.",
            }
            optimizer = SkillTreeOptimizer(
                ScriptedModelClient([ModelReply(content=json.dumps(response), usage={"total_tokens": 42})])
            )

            candidate = optimizer.propose(
                skill,
                [FailureEvidence("cluster", "cross-case", "truncation and unsupported grading")],
                root / "candidates",
                target_scope=(
                    "scripts/collectors/group_b_js.py",
                    "workflow/fast/stage-3-grade.md",
                ),
            )

            self.assertIn("names[:5]", (skill / "scripts/collectors/group_b_js.py").read_text())
            self.assertIn("names[:20]", (candidate.path / "scripts/collectors/group_b_js.py").read_text())
            self.assertIn("not evaluable", (candidate.path / "workflow/fast/stage-3-grade.md").read_text())
            self.assertEqual(("scripts/collectors/group_b_js.py", "workflow/fast/stage-3-grade.md"), candidate.changed_paths)
            manifest = json.loads((candidate.path / "candidate.manifest.json").read_text())
            self.assertEqual(list(candidate.changed_paths), manifest["changed_paths"])
            self.assertEqual("generated-signature\n", (candidate.path / "skill.sig").read_text())

    def test_inventory_exposes_resources_but_protects_generated_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory)
            make_alert_skill(skill)
            inventory = editable_skill_inventory(skill)
            self.assertIn("SKILL.md", inventory)
            self.assertIn("scripts/collectors/group_b_js.py", inventory)
            self.assertIn("workflow/fast/stage-3-grade.md", inventory)
            self.assertNotIn("skill.sig", inventory)

    def test_rejects_invalid_python_and_unapproved_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            make_alert_skill(skill)
            invalid = {
                "changes": [{
                    "path": "scripts/collectors/group_b_js.py",
                    "operation": "replace_text",
                    "old_text": "return names[:5]",
                    "new_text": "return names[",
                    "reason": "bad",
                }]
            }
            with self.assertRaises(CandidateRejected):
                SkillTreeOptimizer(ScriptedModelClient([ModelReply(content=json.dumps(invalid))])).propose(
                    skill,
                    [FailureEvidence("cluster", "grader", "failed")],
                    root / "candidates",
                    target_scope=("scripts/collectors/group_b_js.py",),
                )
            with self.assertRaises(CandidateRejected):
                SkillTreeOptimizer(ScriptedModelClient([])).propose(
                    skill,
                    [FailureEvidence("cluster", "grader", "failed")],
                    root / "other",
                    target_scope=("skill.sig",),
                )

    def test_runs_configuration_owned_validation_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            make_alert_skill(skill)
            response = {"changes": [{
                "path": "config/fast-flow-whitelist.json",
                "operation": "replace_text",
                "old_text": "true",
                "new_text": "false",
                "reason": "disable an unsafe route",
            }]}
            candidate = SkillTreeOptimizer(ScriptedModelClient([ModelReply(content=json.dumps(response))])).propose(
                skill,
                [FailureEvidence("cluster", "grader", "failed")],
                root / "candidates",
                target_scope=("config/fast-flow-whitelist.json",),
                policy=SkillTreePatchPolicy(validation_commands=(("python3", "-m", "json.tool", "config/fast-flow-whitelist.json"),)),
            )
            self.assertEqual(0, candidate.validation[0]["returncode"])

    def test_publisher_commits_all_manifest_declared_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            make_alert_skill(skill)
            subprocess.run(("git", "init", "-b", "feature/lzn/auto-better"), cwd=skill, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(("git", "config", "user.name", "Aceval Test"), cwd=skill, check=True)
            subprocess.run(("git", "config", "user.email", "aceval@example.invalid"), cwd=skill, check=True)
            subprocess.run(("git", "remote", "add", "origin", "ssh://git@git.sankuai.com/nibfe/alert-level-with-kb.git"), cwd=skill, check=True)
            subprocess.run(("git", "add", "."), cwd=skill, check=True)
            subprocess.run(("git", "commit", "-m", "fixture"), cwd=skill, check=True, stdout=subprocess.DEVNULL)
            response = {"changes": [
                {"path": "scripts/collectors/group_b_js.py", "operation": "replace_text", "old_text": "names[:5]", "new_text": "names[:20]", "reason": "coverage"},
                {"path": "workflow/fast/stage-3-grade.md", "operation": "replace_text", "old_text": "low risk", "new_text": "not evaluable", "reason": "evidence"},
            ]}
            candidate = SkillTreeOptimizer(ScriptedModelClient([ModelReply(content=json.dumps(response))])).propose(
                skill,
                [FailureEvidence("cluster", "grader", "failed")],
                root / "candidates",
                target_scope=("scripts/collectors/group_b_js.py", "workflow/fast/stage-3-grade.md"),
            )
            repository = RepositoryConfig(
                ssh_url="ssh://git@git.sankuai.com/nibfe/alert-level-with-kb.git",
                branch="feature/lzn/auto-better",
                local_path=str(skill),
            )
            publisher = NoNetworkPublisher()

            commit = publisher.publish(repository, candidate.path, message="aceval: multi-file repair")

            self.assertEqual(40, len(commit))
            changed = subprocess.run(("git", "show", "--pretty=", "--name-only", "HEAD"), cwd=skill, check=True, stdout=subprocess.PIPE).stdout.decode().splitlines()
            self.assertEqual(set(candidate.changed_paths), set(changed))
            self.assertIn("names[:20]", (skill / "scripts/collectors/group_b_js.py").read_text())

    def test_publisher_commits_greenfield_created_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "empty-skill"
            skill.mkdir()
            (skill / "README.md").write_text("# Empty repository\n", encoding="utf-8")
            subprocess.run(("git", "init", "-b", "feature/create"), cwd=skill, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(("git", "config", "user.name", "Aceval Test"), cwd=skill, check=True)
            subprocess.run(("git", "config", "user.email", "aceval@example.invalid"), cwd=skill, check=True)
            subprocess.run(("git", "remote", "add", "origin", "ssh://git@git.example/new-skill.git"), cwd=skill, check=True)
            subprocess.run(("git", "add", "."), cwd=skill, check=True)
            subprocess.run(("git", "commit", "-m", "empty fixture"), cwd=skill, check=True, stdout=subprocess.DEVNULL)
            policy = SkillTreePatchPolicy()
            parent = scan_skill_tree(skill, policy, require_entrypoint=False)
            changes = [
                {"path": "SKILL.md", "operation": "create_file", "content": "---\nname: demo\ndescription: Demo Skill.\n---\n\n# Demo\n", "reason": "entrypoint"},
                {"path": "references/guide.md", "operation": "create_file", "content": "# Guide\n", "reason": "reference"},
            ]
            candidate = materialize_skill_tree_candidate(
                skill, parent, changes, "greenfield", root / "candidates",
                ("SKILL.md", "references/guide.md"), policy,
                create_paths=("SKILL.md", "references/guide.md"),
            )
            repository = RepositoryConfig(
                ssh_url="ssh://git@git.example/new-skill.git",
                branch="feature/create",
                local_path=str(skill),
            )

            commit = NoNetworkPublisher().publish(repository, candidate.path, message="aceval: create demo")

            self.assertEqual(40, len(commit))
            self.assertTrue((skill / "SKILL.md").is_file())
            changed = subprocess.run(("git", "show", "--pretty=", "--name-only", "HEAD"), cwd=skill, check=True, stdout=subprocess.PIPE).stdout.decode().splitlines()
            self.assertEqual({"SKILL.md", "references/guide.md"}, set(changed))

    def test_rejected_challenger_is_restored_with_an_auditable_revert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            skill = root / "skill"
            subprocess.run(("git", "init", "--bare", str(remote)), check=True, stdout=subprocess.DEVNULL)
            skill.mkdir()
            subprocess.run(("git", "init", "-b", "feature/eval"), cwd=skill, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(("git", "config", "user.name", "Aceval Test"), cwd=skill, check=True)
            subprocess.run(("git", "config", "user.email", "aceval@example.invalid"), cwd=skill, check=True)
            origin = "ssh://git@example.invalid/rejected-candidate.git"
            subprocess.run(("git", "remote", "add", "origin", origin), cwd=skill, check=True)
            (skill / "SKILL.md").write_text("# Champion\n", encoding="utf-8")
            subprocess.run(("git", "add", "SKILL.md"), cwd=skill, check=True)
            subprocess.run(("git", "commit", "-m", "champion"), cwd=skill, check=True, stdout=subprocess.DEVNULL)
            champion = subprocess.run(("git", "rev-parse", "HEAD"), cwd=skill, check=True, stdout=subprocess.PIPE).stdout.decode().strip()
            (skill / "SKILL.md").write_text("# Rejected challenger\n", encoding="utf-8")
            subprocess.run(("git", "add", "SKILL.md"), cwd=skill, check=True)
            subprocess.run(("git", "commit", "-m", "challenger"), cwd=skill, check=True, stdout=subprocess.DEVNULL)
            challenger = subprocess.run(("git", "rev-parse", "HEAD"), cwd=skill, check=True, stdout=subprocess.PIPE).stdout.decode().strip()
            repository = RepositoryConfig(
                ssh_url=origin,
                branch="feature/eval",
                local_path=str(skill),
            )

            publisher = NoNetworkPublisher()
            restored = publisher.restore_rejected_candidate(
                repository,
                challenger_commit=challenger,
                champion_commit=champion,
            )

            self.assertEqual(40, len(restored))
            self.assertNotEqual(champion, restored)
            self.assertEqual("# Champion\n", (skill / "SKILL.md").read_text(encoding="utf-8"))
            subject = subprocess.run(
                ("git", "log", "-1", "--pretty=%s"), cwd=skill, check=True, stdout=subprocess.PIPE
            ).stdout.decode().strip()
            self.assertTrue(subject.startswith("Revert"))
            self.assertEqual(("push", "origin", "HEAD:feature/eval"), publisher.pushed)


if __name__ == "__main__":
    unittest.main()
