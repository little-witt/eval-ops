import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aceval.subjects import SkillMarkdownSubjectAdapter, SubjectValidationError, with_variant


class SkillMarkdownSubjectAdapterTest(unittest.TestCase):
    def test_snapshots_and_materializes_read_only_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = root / "baseline"
            subject.mkdir()
            (subject / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            adapter = SkillMarkdownSubjectAdapter()

            snapshot = with_variant(adapter.snapshot(str(subject)), "candidate")
            target = adapter.materialize(snapshot, root / "materialized")

            self.assertEqual("candidate", snapshot.metadata["variant"])
            self.assertEqual("# Demo\n", (target / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(0, (target / "SKILL.md").stat().st_mode & 0o222)
            self.assertEqual("candidate", adapter.snapshot(str(target)).metadata["variant"])
            self.assertEqual(0, (target / "candidate.json").stat().st_mode & 0o222)

    def test_merges_subject_metadata_and_prefers_manifest_variant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            subject = Path(directory) / "directory-name"
            subject.mkdir()
            (subject / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            (subject / "subject.json").write_text(
                json.dumps(
                    {
                        "id": "demo-skill",
                        "version": "top-level-version",
                        "metadata": {
                            "owner": "eval-team",
                            "variant": "candidate",
                            "version": "metadata-version",
                            "path": "/untrusted/path",
                        },
                    }
                ),
                encoding="utf-8",
            )

            snapshot = SkillMarkdownSubjectAdapter().snapshot(str(subject))

            self.assertEqual("demo-skill", snapshot.metadata["id"])
            self.assertEqual("eval-team", snapshot.metadata["owner"])
            self.assertEqual("metadata-version", snapshot.metadata["version"])
            self.assertEqual("candidate", snapshot.metadata["variant"])
            self.assertEqual(str(subject.resolve()), snapshot.metadata["path"])
            self.assertEqual("SKILL.md", snapshot.metadata["entrypoint"])

    def test_uses_manifest_version_as_variant_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            subject = Path(directory) / "directory-name"
            subject.mkdir()
            (subject / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            (subject / "subject.json").write_text(
                json.dumps({"version": "validation", "metadata": {"owner": "eval-team"}}),
                encoding="utf-8",
            )

            snapshot = SkillMarkdownSubjectAdapter().snapshot(str(subject))

            self.assertEqual("validation", snapshot.metadata["version"])
            self.assertEqual("validation", snapshot.metadata["variant"])

    def test_subject_manifest_changes_snapshot_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            subject = Path(directory) / "baseline"
            subject.mkdir()
            (subject / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            manifest = subject / "subject.json"
            manifest.write_text('{"metadata":{"variant":"baseline"}}', encoding="utf-8")
            adapter = SkillMarkdownSubjectAdapter()

            first = adapter.snapshot(str(subject))
            manifest.write_text('{"metadata":{"variant":"candidate"}}', encoding="utf-8")
            second = adapter.snapshot(str(subject))

            self.assertNotEqual(first.content_hash, second.content_hash)

    def test_materialize_preserves_exact_subject_manifest_and_composite_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = root / "baseline"
            subject.mkdir()
            (subject / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            manifest_bytes = (
                b'{\n  "metadata": {"variant": "baseline", '
                b'"label": "\xe6\xb5\x8b\xe8\xaf\x95"},\n  "version": "1.0"\n}\n'
            )
            (subject / "subject.json").write_bytes(manifest_bytes)
            adapter = SkillMarkdownSubjectAdapter()

            snapshot = adapter.snapshot(str(subject))
            target = adapter.materialize(snapshot, root / "materialized")
            materialized_snapshot = adapter.snapshot(str(target))

            self.assertEqual(manifest_bytes, (target / "subject.json").read_bytes())
            self.assertEqual(snapshot.content_hash, materialized_snapshot.content_hash)
            self.assertEqual(0, (target / "subject.json").stat().st_mode & 0o222)

    def test_single_skill_file_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory) / "SKILL.md"
            content = "# Demo\n"
            skill.write_text(content, encoding="utf-8")

            snapshot = SkillMarkdownSubjectAdapter().snapshot(str(skill))

            self.assertEqual(hashlib.sha256(content.encode("utf-8")).hexdigest(), snapshot.content_hash)
            self.assertEqual(Path(directory).name, snapshot.metadata["variant"])

    def test_rejects_invalid_or_non_object_subject_manifest(self) -> None:
        invalid_values = ("{", "[]", '{"metadata": []}', '{"metadata":{"score":NaN}}')
        for value in invalid_values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                subject = Path(directory)
                (subject / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
                (subject / "subject.json").write_text(value, encoding="utf-8")

                with self.assertRaises(SubjectValidationError):
                    SkillMarkdownSubjectAdapter().snapshot(str(subject))

    def test_rejects_symlinked_subject_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "manifest-target.json"
            target.write_text("{}", encoding="utf-8")
            subject = root / "subject"
            subject.mkdir()
            (subject / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            (subject / "subject.json").symlink_to(target)

            with self.assertRaises(SubjectValidationError):
                SkillMarkdownSubjectAdapter().snapshot(str(subject))

            (subject / "subject.json").unlink()
            (subject / "SKILL.md").unlink()
            skill_target = root / "skill-target.md"
            skill_target.write_text("# Demo\n", encoding="utf-8")
            (subject / "SKILL.md").symlink_to(skill_target)

            with self.assertRaises(SubjectValidationError):
                SkillMarkdownSubjectAdapter().snapshot(str(subject))

    def test_rejects_missing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SubjectValidationError):
                SkillMarkdownSubjectAdapter().snapshot(directory)


if __name__ == "__main__":
    unittest.main()
