import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aceval.subjects import SkillMarkdownSubjectAdapter
from aceval.trial_environment import (
    build_trial_environment_contract,
    comparison_context_hash,
)


class TrialEnvironmentContractTests(unittest.TestCase):
    def test_active_skill_hash_changes_without_breaking_paired_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skill"
            skill.mkdir()
            entrypoint = skill / "SKILL.md"
            entrypoint.write_text("# Skill\n\nReturn grounded output.\n", encoding="utf-8")
            repository = SimpleNamespace(
                local_path=str(skill),
                ssh_url="ssh://git@example/skill.git",
                branch="feature/eval",
                mount_path="/workspace/skill",
            )
            config = SimpleNamespace(
                trial_executor=None,
                remote_agent=SimpleNamespace(profile_path=None),
                skill_repository=repository,
                code_repository=None,
            )
            design = {"cases": [{"id": "case-1", "case_revision": "revision-1"}]}

            champion = build_trial_environment_contract(
                config,
                design,
                {"challenger_commit": None},
                iteration=0,
            )
            champion_digest = SkillMarkdownSubjectAdapter().snapshot(str(skill)).content_hash
            self.assertEqual("sha256:" + champion_digest, champion["skill"]["subject_hash"])

            entrypoint.write_text(
                "# Skill\n\nReturn grounded output with a source reference.\n",
                encoding="utf-8",
            )
            challenger = build_trial_environment_contract(
                config,
                design,
                {"challenger_commit": "b" * 40},
                iteration=1,
            )

            self.assertNotEqual(
                champion["skill"]["subject_hash"],
                challenger["skill"]["subject_hash"],
            )
            self.assertNotEqual(champion["contract_hash"], challenger["contract_hash"])
            self.assertEqual(
                comparison_context_hash(champion),
                comparison_context_hash(challenger),
            )


if __name__ == "__main__":
    unittest.main()
