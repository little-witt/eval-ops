import unittest

from aceval.subjects import EvaluationSubject, SubjectContractError, agent_subject, skill_subject


class SubjectContractTests(unittest.TestCase):
    def test_skill_and_agent_have_the_same_immutable_kernel_identity_shape(self):
        skill = skill_subject(repository="ssh://git@example/skill.git", revision="a" * 40, resource_refs=("SKILL.md", "scripts/check.py"))
        agent = agent_subject(agent_id="agent-1", revision="config-v3", environment_id="env-1", model_id="model-1", tool_manifest_hash="sha256:tools")
        self.assertEqual("skill", skill.kind)
        self.assertEqual("agent", agent.kind)
        self.assertTrue(skill.fingerprint.startswith("sha256:"))
        self.assertTrue(agent.fingerprint.startswith("sha256:"))
        self.assertEqual(agent, EvaluationSubject.from_mapping(agent.to_dict()))

    def test_fingerprint_tampering_is_rejected(self):
        value = dict(skill_subject(repository="repo", revision="one").to_dict())
        value["revision"] = "two"
        with self.assertRaises(SubjectContractError):
            EvaluationSubject.from_mapping(value)


if __name__ == "__main__":
    unittest.main()
