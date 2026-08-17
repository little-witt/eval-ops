from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aceval.pack_lifecycle import (
    CALIBRATION_DRAFT,
    CALIBRATION_FROZEN,
    CALIBRATION_LEGACY,
    PACK_LOCK_FILE,
    PackLifecycleError,
    pack_calibration_status,
    verify_pack_lifecycle,
    write_pack_lock,
)


class PackLifecycleTests(unittest.TestCase):
    def test_legacy_and_draft_statuses_do_not_imply_frozen_trust(self):
        self.assertEqual(
            CALIBRATION_LEGACY,
            pack_calibration_status(type("Pack", (), {"metadata": {}})()),
        )
        self.assertEqual(
            CALIBRATION_DRAFT,
            pack_calibration_status(
                type(
                    "Pack",
                    (),
                    {"metadata": {"calibration_status": CALIBRATION_DRAFT}},
                )()
            ),
        )

    def test_frozen_lock_detects_post_freeze_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pack.yaml").write_text("{}\n", encoding="utf-8")
            (root / "scenario.json").write_text("{}\n", encoding="utf-8")
            write_pack_lock(root, "0.1.0")

            status = verify_pack_lifecycle(
                root, {"calibration_status": CALIBRATION_FROZEN}
            )
            self.assertEqual(CALIBRATION_FROZEN, status)

            (root / "scenario.json").write_text('{"changed":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                PackLifecycleError, "contents differ from its calibration lock"
            ):
                verify_pack_lifecycle(
                    root, {"calibration_status": CALIBRATION_FROZEN}
                )

    def test_draft_cannot_retain_a_stale_freeze_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pack.yaml").write_text("{}\n", encoding="utf-8")
            (root / PACK_LOCK_FILE).write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(PackLifecycleError, "only valid"):
                verify_pack_lifecycle(
                    root, {"calibration_status": CALIBRATION_DRAFT}
                )


if __name__ == "__main__":
    unittest.main()
