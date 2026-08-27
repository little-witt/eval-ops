import unittest

from aceval.kernel_contracts import (
    KERNEL_CONFIG_API_VERSION,
    KERNEL_INPUT_API_VERSION,
    KernelConfig,
    KernelContractError,
    KernelInput,
)


def config_payload():
    return {
        "api_version": KERNEL_CONFIG_API_VERSION,
        "skill_repository": {
            "ssh_url": "ssh://git@git.example/skills/reviewer.git",
            "branch": "feature/eval",
            "local_path": "/tmp/reviewer",
            "authorization_token_env": "REPOSITORY_PAT",
            "mount_path": "/workspace/skill",
        },
        "code_repository": {
            "ssh_url": "ssh://git@git.example/fixtures/review.git",
            "branch": "case/frontend-001",
            "authorization_token_env": "REPOSITORY_PAT",
            "mount_path": "/workspace/repo",
        },
        "local_analysis": {
            "model_command": ["model-bridge", "--json"],
            "model_id": "analysis-model",
            "api_base_url_env": "MODEL_API_BASE",
            "api_key_env": "MODEL_API_KEY",
            "env_allowlist": ["MODEL_API_BASE", "MODEL_API_KEY"],
        },
        "remote_agent": {"profile_path": "/tmp/catx-profile.json"},
        "policy": {"max_generated_cases": 8, "max_rounds": 4},
    }


class KernelContractTests(unittest.TestCase):
    def test_configuration_round_trip_persists_references_not_secrets(self):
        config = KernelConfig.from_mapping(config_payload())
        round_trip = KernelConfig.from_mapping(config.to_dict())
        self.assertEqual(config, round_trip)
        self.assertEqual("REPOSITORY_PAT", round_trip.code_repository.authorization_token_env)
        self.assertNotIn("authorization_token\"", str(round_trip.to_dict()))

    def test_role_oriented_executors_round_trip_without_legacy_ambiguity(self):
        payload = config_payload()
        payload.pop("remote_agent")
        payload["trial_executor"] = {
            "provider": "catx",
            "profile_path": "/tmp/catx-profile.json",
        }
        payload["analysis_executor"] = {"provider": "local-forge"}
        config = KernelConfig.from_mapping(payload)
        saved = config.to_dict()
        self.assertNotIn("remote_agent", saved)
        self.assertEqual("catx", saved["trial_executor"]["provider"])
        self.assertEqual("local-forge", saved["analysis_executor"]["provider"])
        self.assertEqual(config, KernelConfig.from_mapping(saved))

    def test_executor_schema_conflicts_and_unsupported_trial_provider_fail_closed(self):
        conflict = config_payload()
        conflict["trial_executor"] = {"provider": "catx", "profile_path": "/tmp/catx-profile.json"}
        with self.assertRaisesRegex(KernelContractError, "cannot be configured together"):
            KernelConfig.from_mapping(conflict)
        unsupported = config_payload()
        unsupported.pop("remote_agent")
        unsupported["trial_executor"] = {"provider": "local", "profile_path": "/tmp/local.json"}
        with self.assertRaisesRegex(KernelContractError, "P0 requires catx"):
            KernelConfig.from_mapping(unsupported)

    def test_rejects_raw_credentials_and_non_ssh_repository(self):
        for mutation in (
            {"authorization_token": "secret"},
            {"ssh_url": "https://git.example/skill.git"},
        ):
            payload = config_payload()
            payload["skill_repository"].update(mutation)
            with self.assertRaises(KernelContractError):
                KernelConfig.from_mapping(payload)

    def test_input_supports_all_three_user_effort_modes_and_round_trip(self):
        case_input = KernelInput.from_mapping(
            {
                "api_version": KERNEL_INPUT_API_VERSION,
                "skill_name": "reviewer",
                "cases": [{"id": "case-1", "prompt": "Review this PR", "expected_output": {"issues": 1}}],
            }
        )
        goal_input = KernelInput.from_mapping(
            {"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "reviewer", "goal": "Find important defects"}
        )
        exploratory = KernelInput.from_mapping(
            {"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "reviewer"}
        )
        custom = KernelInput.from_mapping(
            {"api_version": KERNEL_INPUT_API_VERSION, "skill_name": "reviewer", "evalpack_path": "/tmp/custom-pack"}
        )
        self.assertEqual("cases_with_expected", case_input.mode)
        self.assertEqual("goal_only", goal_input.mode)
        self.assertEqual("exploratory", exploratory.mode)
        self.assertEqual("custom_evalpack", custom.mode)
        self.assertEqual(case_input, KernelInput.from_mapping(case_input.to_dict()))
        self.assertTrue(exploratory.effective_goal)
        self.assertEqual(3, len(exploratory.effective_standards))


if __name__ == "__main__":
    unittest.main()
