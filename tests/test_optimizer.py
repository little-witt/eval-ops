import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aceval.agent_runtime import ModelReply, ScriptedModelClient
from aceval.contracts import PatchConstraints, SubjectSnapshot
from aceval.optimizer import (
    CandidateRejected,
    FailureEvidence,
    FrozenCandidateOptimizer,
    SkillMarkdownOptimizer,
    SkillOptimizerBridge,
    SkillPatchPolicy,
)
from aceval.subjects import SkillMarkdownSubjectAdapter


class SkillMarkdownOptimizerTest(unittest.TestCase):
    def test_bridge_uses_frozen_parent_instead_of_mutable_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            candidate = root / "candidate"
            output = root / "output"
            source.mkdir()
            candidate.mkdir()
            original = "# Frozen\n\nOriginal instruction.\n"
            replacement = "# Frozen\n\nImproved instruction.\n"
            (source / "SKILL.md").write_text(original, encoding="utf-8")
            (candidate / "SKILL.md").write_text(replacement, encoding="utf-8")
            snapshot = SubjectSnapshot(
                kind="skill",
                uri=str(source),
                content_hash=hashlib.sha256(original.encode("utf-8")).hexdigest(),
                content=original,
            )
            (source / "SKILL.md").write_text(
                "# Mutable\n\nChanged after snapshot.\n", encoding="utf-8"
            )
            bridge = SkillOptimizerBridge(
                FrozenCandidateOptimizer(candidate),
                output,
                SkillPatchPolicy(),
            )

            patches = asyncio.run(
                bridge.propose(
                    snapshot,
                    (object(),),
                    PatchConstraints(allowed_paths=("SKILL.md",)),
                )
            )

            self.assertEqual(1, len(patches))
            self.assertIn("-Original instruction.", patches[0].unified_diff)
            self.assertNotIn("Changed after snapshot", patches[0].unified_diff)
            self.assertFalse(
                any(path.name.startswith("frozen-parent-") for path in output.iterdir())
            )

    def test_creates_immutable_candidate_without_overwriting_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = root / "subject"
            subject.mkdir()
            original = "# Demo\n\nReturn JSON.\n"
            revised = "# Demo\n\nReturn JSON.\n\nAlways validate required fields.\n"
            (subject / "SKILL.md").write_text(original, encoding="utf-8")
            client = ScriptedModelClient(
                [ModelReply(content=json.dumps({"skill_markdown": revised, "rationale": "missing validation"}))]
            )
            optimizer = SkillMarkdownOptimizer(client)

            snapshot = optimizer.propose(
                subject,
                [FailureEvidence("dev-1", "schema", "required field missing")],
                root / "candidates",
            )

            self.assertEqual(original, (subject / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(revised, (snapshot.path / "SKILL.md").read_text(encoding="utf-8"))
            self.assertIn("+Always validate required fields.", snapshot.patch)

    def test_candidate_preserves_manifest_and_uses_composite_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = root / "subject"
            subject.mkdir()
            original = "# Demo\n\nReturn JSON.\n"
            revised = "# Demo\n\nReturn validated JSON.\n"
            manifest_bytes = b'{\n  "metadata": {"variant": "baseline"}\n}\n'
            (subject / "SKILL.md").write_text(original, encoding="utf-8")
            (subject / "subject.json").write_bytes(manifest_bytes)
            optimizer = SkillMarkdownOptimizer(
                ScriptedModelClient(
                    [ModelReply(content=json.dumps({"skill_markdown": revised}))]
                )
            )

            candidate = optimizer.propose(
                subject,
                [FailureEvidence("dev-1", "schema", "required field missing")],
                root / "candidates",
            )
            frozen_candidate = SkillMarkdownSubjectAdapter().snapshot(
                str(candidate.path)
            )

            self.assertEqual(
                manifest_bytes, (candidate.path / "subject.json").read_bytes()
            )
            self.assertEqual(frozen_candidate.content_hash, candidate.subject_hash)
            self.assertEqual("candidate", frozen_candidate.metadata["variant"])
            self.assertNotEqual(
                hashlib.sha256(revised.encode("utf-8")).hexdigest(),
                candidate.subject_hash,
            )

    def test_bridge_preserves_frozen_parent_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            candidate_source = root / "candidate"
            output = root / "output"
            source.mkdir()
            candidate_source.mkdir()
            original = "# Demo\n\nReturn JSON.\n"
            revised = "# Demo\n\nReturn validated JSON.\n"
            manifest_bytes = b'{\n  "metadata": {"variant": "baseline"}\n}\n'
            (source / "SKILL.md").write_text(original, encoding="utf-8")
            (source / "subject.json").write_bytes(manifest_bytes)
            (candidate_source / "SKILL.md").write_text(revised, encoding="utf-8")
            adapter = SkillMarkdownSubjectAdapter()
            parent = adapter.snapshot(str(source))
            bridge = SkillOptimizerBridge(
                FrozenCandidateOptimizer(candidate_source),
                output,
                SkillPatchPolicy(),
            )

            patches = asyncio.run(
                bridge.propose(
                    parent,
                    (object(),),
                    PatchConstraints(allowed_paths=("SKILL.md",)),
                )
            )
            candidate_path = Path(patches[0].metadata["path"])
            candidate = adapter.snapshot(str(candidate_path))

            self.assertEqual(
                manifest_bytes, (candidate_path / "subject.json").read_bytes()
            )
            self.assertEqual(candidate.content_hash, patches[0].metadata["subject_hash"])
            self.assertEqual(parent.content_hash, patches[0].base_hash)

    def test_rejects_test_literal_leak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = root / "subject"
            subject.mkdir()
            (subject / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            client = ScriptedModelClient(
                [ModelReply(content=json.dumps({"skill_markdown": "# Demo\nsecret-fixture-value\n"}))]
            )

            with self.assertRaises(CandidateRejected):
                SkillMarkdownOptimizer(client).propose(
                    subject,
                    [FailureEvidence("dev-1", "schema", "failed")],
                    root / "candidates",
                    forbidden_literals=["secret-fixture-value"],
                )

    def test_rejects_large_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = root / "subject"
            subject.mkdir()
            (subject / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            candidate = "# Demo\n" + "\n".join("line %s" % index for index in range(5))
            client = ScriptedModelClient(
                [ModelReply(content=json.dumps({"skill_markdown": candidate}))]
            )

            with self.assertRaises(CandidateRejected):
                SkillMarkdownOptimizer(client).propose(
                    subject,
                    [FailureEvidence("dev-1", "schema", "failed")],
                    root / "candidates",
                    policy=SkillPatchPolicy(max_added_lines=2),
                )


if __name__ == "__main__":
    unittest.main()
