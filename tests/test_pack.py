import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aceval.pack import (
    ComponentRegistry,
    DuplicateComponentError,
    EvalPackLoader,
    MissingYamlDependencyError,
    PackFormatError,
    PackPathError,
    PackValidationError,
    UnknownComponentError,
    UnsupportedApiVersionError,
)


class DummyComponent:
    supported_kinds = ("skill",)


def manifest_document():
    return {
        "api_version": "aceval.dev/v1alpha1",
        "kind": "EvalPack",
        "metadata": {"name": "example", "version": "0.1.0"},
        "subject_contract": {
            "kinds": ["skill"],
            "adapter": "skill_markdown_v1",
            "entrypoint": "SKILL.md",
        },
        "driver": {
            "type": "artifact_workspace",
            "required_runtime_capabilities": ["fresh_session"],
        },
        "suite": {
            "dev": "scenarios/dev.yaml",
            "validation_ref": "scenarios/validation.yaml",
            "holdout_ref": "external-holdout-v1",
        },
        "graders": [
            {
                "id": "output-schema",
                "type": "json_schema",
                "hard": True,
                "params": {"schema_ref": "schemas/output.json"},
            }
        ],
        "optimizer_policy": {
            "adapter": "skill_markdown_v1",
            "allowed_paths": ["SKILL.md"],
            "visible_splits": ["dev"],
        },
    }


def scenario_document(split, scenario_id, fixture, oracle):
    return {
        "scenarios": [
            {
                "id": scenario_id,
                "split": split,
                "prompt": "Produce JSON.",
                "fixtures": [fixture],
                "oracle_ref": oracle,
                "grader_ids": ["output-schema"],
                "timeout_seconds": 10,
            }
        ]
    }


class PackTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "pack"
        for directory in ("scenarios", "fixtures", "oracles", "schemas"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.write_json("pack.yaml", manifest_document())
        self.write_json(
            "scenarios/dev.yaml",
            scenario_document("dev", "dev-1", "fixtures/input.csv", "oracles/dev.json"),
        )
        self.write_json(
            "scenarios/validation.yaml",
            scenario_document(
                "validation",
                "validation-1",
                "fixtures/validation.csv",
                "oracles/validation.json",
            ),
        )
        (self.root / "fixtures/input.csv").write_text("amount\n10\n", encoding="utf-8")
        (self.root / "fixtures/validation.csv").write_text("amount\n20\n", encoding="utf-8")
        self.write_json("oracles/dev.json", {"total": 10})
        self.write_json("oracles/validation.json", [{"total": 20}])
        self.write_json("schemas/output.json", {"type": "object"})

    def tearDown(self):
        self.temporary.cleanup()

    def write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def registry(self):
        registry = ComponentRegistry()
        registry.register_subject_adapter("skill_markdown_v1", DummyComponent())
        registry.register_driver("artifact_workspace", DummyComponent())
        registry.register_grader("json_schema", DummyComponent())
        registry.register_optimizer("skill_markdown_v1", DummyComponent())
        return registry

    def test_load_freezes_local_refs_oracles_and_hashes(self):
        loader = EvalPackLoader(self.registry())

        first = loader.load(self.root)
        self.assertEqual(len(first.scenarios), 2)
        self.assertEqual(
            {"type": "object"}, dict(first.resources["schemas/output.json"])
        )
        with self.assertRaises(TypeError):
            first.resources["schemas/output.json"]["type"] = "array"
        self.assertEqual(first.scenario_by_id("validation-1").split, "validation")
        self.assertEqual(first.oracles["oracles/validation.json"].data, ({"total": 20},))
        with self.assertRaises(TypeError):
            first.file_hashes["pack.yaml"] = "forged"
        with self.assertRaises(TypeError):
            first.file_hashes |= {"pack.yaml": "forged"}
        with self.assertRaises(TypeError):
            dict.__setitem__(first.file_hashes, "pack.yaml", "forged")
        with self.assertRaises(TypeError):
            first.oracles["oracles/validation.json"].data[0]["total"] = 999
        self.assertIn("fixtures/input.csv", first.file_hashes)
        self.assertEqual(len(first.pack_hash), 64)
        self.assertEqual(len(first.suite_hash), 64)

        (self.root / "fixtures/input.csv").write_text("amount\n99\n", encoding="utf-8")
        second = loader.load(self.root)
        self.assertNotEqual(first.pack_hash, second.pack_hash)
        self.assertNotEqual(
            first.scenario_by_id("dev-1").content_hash,
            second.scenario_by_id("dev-1").content_hash,
        )

    def test_unknown_component_fails_closed(self):
        registry = self.registry()
        manifest = manifest_document()
        manifest["graders"][0]["type"] = "not_registered"
        self.write_json("pack.yaml", manifest)

        with self.assertRaises(PackValidationError) as caught:
            EvalPackLoader(registry).load(self.root)

        self.assertIn("unknown_grader", {issue.code for issue in caught.exception.report.errors})

    def test_scenario_cannot_select_undeclared_grader(self):
        document = scenario_document(
            "dev", "dev-1", "fixtures/input.csv", "oracles/dev.json"
        )
        document["scenarios"][0]["grader_ids"] = ["missing"]
        self.write_json("scenarios/dev.yaml", document)

        with self.assertRaises(PackValidationError) as caught:
            EvalPackLoader(self.registry()).load(self.root)

        self.assertIn(
            "unknown_scenario_grader",
            {issue.code for issue in caught.exception.report.errors},
        )

    def test_each_scenario_requires_at_least_one_hard_grader(self):
        manifest = manifest_document()
        manifest["graders"][0]["hard"] = False
        self.write_json("pack.yaml", manifest)

        with self.assertRaises(PackValidationError) as caught:
            EvalPackLoader(self.registry()).load(self.root)

        self.assertIn(
            "scenario_without_hard_grader",
            {issue.code for issue in caught.exception.report.errors},
        )

    def test_unsupported_api_version_fails_closed(self):
        manifest = manifest_document()
        manifest["api_version"] = "aceval.dev/v999"
        self.write_json("pack.yaml", manifest)

        with self.assertRaises(UnsupportedApiVersionError):
            EvalPackLoader().load(self.root)

    def test_fixture_path_traversal_is_rejected(self):
        document = scenario_document(
            "dev", "dev-1", "../outside.txt", "oracles/dev.json"
        )
        self.write_json("scenarios/dev.yaml", document)

        with self.assertRaises(PackPathError):
            EvalPackLoader().load(self.root)

    def test_scenario_id_must_be_a_safe_component(self):
        document = scenario_document(
            "dev", "../../outside", "fixtures/input.csv", "oracles/dev.json"
        )
        self.write_json("scenarios/dev.yaml", document)

        with self.assertRaises(PackFormatError):
            EvalPackLoader().load(self.root)

    def test_manifest_schema_ref_path_traversal_is_rejected(self):
        manifest = manifest_document()
        manifest["graders"][0]["params"]["schema_ref"] = "../schema.json"
        self.write_json("pack.yaml", manifest)

        with self.assertRaises(PackPathError):
            EvalPackLoader().load(self.root)

    def test_manifest_rejects_local_and_opaque_ref_for_same_split(self):
        for split in ("validation", "holdout"):
            with self.subTest(split=split):
                manifest = manifest_document()
                manifest["suite"][split] = "scenarios/validation.yaml"
                self.write_json("pack.yaml", manifest)

                with self.assertRaises(PackFormatError):
                    EvalPackLoader().load(self.root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are not supported")
    def test_symlink_is_rejected_even_when_it_resolves_inside_pack(self):
        link = self.root / "fixtures/link.csv"
        try:
            link.symlink_to(self.root / "fixtures/input.csv")
        except OSError as exc:
            self.skipTest("cannot create symlink: {0}".format(exc))

        with self.assertRaises(PackPathError):
            EvalPackLoader().load(self.root)

    def test_non_json_yaml_has_clear_optional_dependency_error(self):
        (self.root / "pack.yaml").write_text(
            "api_version: aceval.dev/v1alpha1\nkind: EvalPack\n", encoding="utf-8"
        )
        real_import = importlib.import_module

        def import_without_yaml(name, package=None):
            if name == "yaml":
                raise ModuleNotFoundError("No module named 'yaml'")
            return real_import(name, package)

        with mock.patch("aceval.pack.importlib.import_module", side_effect=import_without_yaml):
            with self.assertRaises(MissingYamlDependencyError) as caught:
                EvalPackLoader().load(self.root)

        self.assertIn("aceval[yaml]", str(caught.exception))

    def test_non_finite_json_constants_are_not_reinterpreted_as_yaml(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                manifest = manifest_document()
                manifest["metadata"]["description"] = "NON_FINITE_SENTINEL"
                document = json.dumps(manifest).replace(
                    '"NON_FINITE_SENTINEL"', constant
                )
                (self.root / "pack.yaml").write_text(document, encoding="utf-8")

                with mock.patch("aceval.pack.importlib.import_module") as yaml_import:
                    with self.assertRaises(PackFormatError):
                        EvalPackLoader(self.registry()).load(self.root)

                yaml_import.assert_not_called()


class RegistryTests(unittest.TestCase):
    def test_registry_is_explicit_and_categories_are_isolated(self):
        registry = ComponentRegistry()
        driver = DummyComponent()
        registry.register_driver("workspace", driver)

        self.assertIs(registry.driver("workspace"), driver)
        with self.assertRaises(UnknownComponentError):
            registry.grader("workspace")
        with self.assertRaises(DuplicateComponentError):
            registry.register_driver("workspace", DummyComponent())


if __name__ == "__main__":
    unittest.main()
