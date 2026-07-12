import os
import tempfile
import unittest
from pathlib import Path

from patent_factory.privacy import (
    DEFAULT_RETENTION,
    DataClass,
    EgressApproval,
    PrivacyError,
    assert_canaries_absent,
    delete_run,
    guarded_hosted_call,
    redact_mapping,
    secret_status,
)


def approval(**changes):
    values = {
        "decision_id": "dec_1",
        "subject_revision_hash": "revision-1",
        "recipient": "hosted.example",
        "model_class": "hosted-model",
        "purpose": "review",
        "approval_scope": "fields:name,domain",
        "approved_data_classes": (DataClass.CONFIDENTIAL,),
    }
    values.update(changes)
    return EgressApproval(**values)


class PrivacyTests(unittest.TestCase):
    def test_data_classes_have_explicit_retention_defaults(self):
        self.assertEqual(set(DEFAULT_RETENTION), set(DataClass))

    def test_secret_status_never_returns_value(self):
        status = secret_status("KIPRIS_PLUS_API_KEY", {"KIPRIS_PLUS_API_KEY": "CANARY-SECRET"})
        self.assertEqual(status, {"name": "KIPRIS_PLUS_API_KEY", "present": True, "status": "configured"})
        self.assertNotIn("CANARY-SECRET", repr(status))

    def test_hosted_callback_is_blocked_without_current_exact_approval(self):
        calls = []

        def callback(manifest):
            calls.append(manifest.manifest_id)
            return "called"

        arguments = {
            "callback": callback,
            "subject_revision_hash": "revision-1",
            "recipient": "hosted.example",
            "model_class": "hosted-model",
            "purpose": "review",
            "approval_scope": "fields:name,domain",
            "data_classes": (DataClass.CONFIDENTIAL,),
            "content_hashes": ("hash-b", "hash-a"),
            "payload": {"name": "redacted fixture"},
        }
        with self.assertRaisesRegex(PrivacyError, "approval_required"):
            guarded_hosted_call(approval=None, **arguments)
        with self.assertRaisesRegex(PrivacyError, "subject_revision_hash_mismatch"):
            guarded_hosted_call(approval=approval(subject_revision_hash="old"), **arguments)
        with self.assertRaisesRegex(PrivacyError, "stale_decision"):
            guarded_hosted_call(approval=approval(current=False), **arguments)
        self.assertEqual(calls, [])

        result, manifest = guarded_hosted_call(approval=approval(), **arguments)
        self.assertEqual(result, "called")
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest.content_hashes, ("hash-a", "hash-b"))
        self.assertEqual(manifest.decision_id, "dec_1")

    def test_canary_blocks_callback_and_error_does_not_echo_value(self):
        calls = []
        with self.assertRaises(PrivacyError) as captured:
            guarded_hosted_call(
                lambda manifest: calls.append(manifest),
                approval=approval(),
                subject_revision_hash="revision-1",
                recipient="hosted.example",
                model_class="hosted-model",
                purpose="review",
                approval_scope="fields:name,domain",
                data_classes=(DataClass.CONFIDENTIAL,),
                content_hashes=("hash",),
                payload={"profile": "PRIVATE-CANARY"},
                canaries=("PRIVATE-CANARY",),
            )
        self.assertNotIn("PRIVATE-CANARY", str(captured.exception))
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(PrivacyError, "canary_detected"):
            assert_canaries_absent("x SECRET x", ("SECRET",), boundary="log")

    def test_redaction_removes_secret_and_proprietary_fields(self):
        redacted = redact_mapping({
            "api_key": "SECRET",
            "nested": {"raw_document": "PRIVATE", "status": "ok"},
            "public": "visible",
        })
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["raw_document"], "[REDACTED]")
        self.assertEqual(redacted["public"], "visible")
        self.assertNotIn("SECRET", repr(redacted))
        self.assertNotIn("PRIVATE", repr(redacted))

    def test_delete_run_never_follows_links_or_touches_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary, "workspace")
            run = workspace / "runs" / "run-1"
            sibling = workspace / "runs" / "run-2"
            outside = Path(temporary, "outside")
            run.mkdir(parents=True)
            sibling.mkdir()
            outside.mkdir()
            (run / "factory.sqlite3").write_text("private", encoding="utf-8")
            (sibling / "keep").write_text("keep", encoding="utf-8")
            (outside / "keep").write_text("outside", encoding="utf-8")
            link = run / "outside-link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")

            report = delete_run(run, workspace)
            self.assertTrue(report.complete, report.failures)
            self.assertFalse(run.exists())
            self.assertEqual((sibling / "keep").read_text(encoding="utf-8"), "keep")
            self.assertEqual((outside / "keep").read_text(encoding="utf-8"), "outside")

    def test_delete_run_rejects_root_symlink_and_outside_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary, "workspace")
            real = workspace / "real-run"
            real.mkdir(parents=True)
            alias = workspace / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(PrivacyError, "symlink_root_rejected"):
                delete_run(alias, workspace)
            with self.assertRaisesRegex(PrivacyError, "path_outside_workspace"):
                delete_run(Path(temporary), workspace)


    def test_existing_private_root_is_hardened_to_owner_only(self):
        from patent_factory.paths import private_root

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = root / "documents"
            documents.mkdir(mode=0o755)
            documents.chmod(0o755)
            previous = Path.cwd()
            try:
                os.chdir(root)
                private_root(Path("documents"), "documents root")
            finally:
                os.chdir(previous)
            if os.name == "posix":
                self.assertEqual(documents.stat().st_mode & 0o777, 0o700)

if __name__ == "__main__":
    unittest.main()
