import json
import tempfile
import unittest
from pathlib import Path

from aceval.environment_contracts import (
    CANDIDATE_BUNDLE_API_VERSION,
    ENVIRONMENT_BLUEPRINT_API_VERSION,
    CommandSpec,
    EnvironmentBlueprint,
    EnvironmentContractError,
    create_candidate_bundle,
    verify_candidate_bundle,
)
from aceval.contracts import as_primitive


DIGEST = "sha256:" + "a" * 64


class EnvironmentContractTests(unittest.TestCase):
    def test_blueprint_is_content_addressed_and_rejects_mutable_images(self):
        blueprint = EnvironmentBlueprint(
            api_version=ENVIRONMENT_BLUEPRINT_API_VERSION,
            id="test/v1",
            image=DIGEST,
            capabilities=("python",),
            validation_commands=(CommandSpec(argv=("python", "--version")),),
        )
        restored = EnvironmentBlueprint.from_mapping(
            json.loads(json.dumps(as_primitive(blueprint)))
        )
        self.assertEqual(blueprint.blueprint_hash, restored.blueprint_hash)
        with self.assertRaisesRegex(EnvironmentContractError, "SHA-256"):
            EnvironmentBlueprint(
                api_version=ENVIRONMENT_BLUEPRINT_API_VERSION,
                id="bad/v1",
                image="python:latest",
                capabilities=("python",),
                validation_commands=(CommandSpec(argv=("python", "--version")),),
            )

    def test_candidate_bundle_detects_content_drift_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "repo")
            root.mkdir()
            Path(root, "a.py").write_text("x = 1\n", encoding="utf-8")
            bundle = create_candidate_bundle(
                root,
                subject_hash=DIGEST,
                producer_run_id="run-1",
            )
            self.assertEqual(CANDIDATE_BUNDLE_API_VERSION, bundle.api_version)
            self.assertEqual(root.resolve(), verify_candidate_bundle(bundle))
            Path(root, "a.py").write_text("x = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(EnvironmentContractError, "does not match"):
                verify_candidate_bundle(bundle)
            Path(root, "link").symlink_to(Path(root, "a.py"))
            with self.assertRaisesRegex(EnvironmentContractError, "symlink"):
                create_candidate_bundle(
                    root,
                    subject_hash=DIGEST,
                    producer_run_id="run-2",
                )

    def test_contract_loader_rejects_duplicate_keys_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "blueprint.json")
            path.write_text('{"api_version":"x","api_version":"y"}', encoding="utf-8")
            with self.assertRaisesRegex(EnvironmentContractError, "duplicate"):
                EnvironmentBlueprint.load(path)
        with self.assertRaisesRegex(EnvironmentContractError, "unsupported fields"):
            EnvironmentBlueprint.from_mapping(
                {
                    "api_version": ENVIRONMENT_BLUEPRINT_API_VERSION,
                    "id": "test/v1",
                    "image": DIGEST,
                    "capabilities": ["python"],
                    "validation_commands": [{"argv": ["python", "--version"]}],
                    "unexpected": True,
                }
            )


if __name__ == "__main__":
    unittest.main()
