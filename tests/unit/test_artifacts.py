import os
import stat
import tempfile
import unittest
from pathlib import Path

from patent_factory.artifacts import (
    ArtifactError,
    canonical_json_bytes,
    export_immutable,
    export_immutable_json,
    recover_artifact_exports,
)


class ArtifactTests(unittest.TestCase):
    def test_canonical_json_export_is_stable_private_and_no_clobber(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "revision.json")
            first = export_immutable_json(target, {"한글": "e\u0301\r\nline", "a": 1})
            second = export_immutable_json(target, {"a": 1, "한글": "é\nline"})
            self.assertEqual(first.artifact_id, second.artifact_id)
            self.assertTrue(second.reused)
            self.assertEqual(target.read_bytes(), canonical_json_bytes({"a": 1, "한글": "é\nline"}))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            with self.assertRaisesRegex(ArtifactError, "immutable path"):
                export_immutable_json(target, {"a": 2})

    def test_fault_before_publish_leaves_no_target_or_temp(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "revision.json")

            def fail(stage):
                if stage == "file_fsynced":
                    raise RuntimeError("injected")

            with self.assertRaisesRegex(RuntimeError, "injected"):
                export_immutable(target, b"content", fault_hook=fail)
            self.assertFalse(target.exists())
            self.assertEqual(tuple(Path(temporary).glob(".artifact-*.tmp")), ())

    def test_conflicting_publish_does_not_leave_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "revision.json"

            def concurrent_publish(stage):
                if stage == "file_fsynced":
                    target.write_bytes(b"prior")

            with self.assertRaisesRegex(ArtifactError, "concurrent immutable publish"):
                export_immutable(target, b"changed", fault_hook=concurrent_publish)
            self.assertEqual(tuple(directory.glob(".artifact-*.tmp")), ())

    def test_fault_after_publish_leaves_valid_target_and_recoverable_temp(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "revision.json"

            def fail(stage):
                if stage == "published":
                    raise RuntimeError("injected")

            with self.assertRaisesRegex(RuntimeError, "injected"):
                export_immutable(target, b"content", fault_hook=fail)
            self.assertEqual(target.read_bytes(), b"content")
            self.assertEqual(len(tuple(directory.glob(".artifact-*.tmp"))), 1)
            self.assertEqual(len(recover_artifact_exports(directory)), 1)
            self.assertEqual(tuple(directory.glob(".artifact-*.tmp")), ())
            self.assertTrue(export_immutable(target, b"content").reused)

    def test_target_and_ancestor_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            target = real / "artifact.json"
            alias = root / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ArtifactError, "symbolic link"):
                export_immutable(alias / "artifact.json", b"content")
            target.write_bytes(b"content")
            leaf = real / "leaf.json"
            leaf.symlink_to(target)
            with self.assertRaisesRegex(ArtifactError, "symbolic link"):
                export_immutable(leaf, b"content")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO required")
    def test_nonregular_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "artifact.json")
            os.mkfifo(target)
            with self.assertRaisesRegex(ArtifactError, "regular file"):
                export_immutable(target, b"content")


if __name__ == "__main__":
    unittest.main()
