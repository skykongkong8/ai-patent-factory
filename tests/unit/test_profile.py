import json
import os
import tempfile
import unittest
from pathlib import Path

from patent_factory.profile import IncomingFact, document_facts, empty_profile, folder_facts, interview_facts, merge_profile
from patent_factory.provenance import Claim, EpistemicLabel


class ProfileTests(unittest.TestCase):
    def test_document_repeat_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "profile.md")
            path.write_text("technical_domain: 산업용 AI\nexpertise: 센서 분석\n", encoding="utf-8")
            first, conflicts, changes = merge_profile(empty_profile(), document_facts(path))
            self.assertFalse(conflicts)
            self.assertEqual(changes, 2)
            second, conflicts, changes = merge_profile(first, document_facts(path))
            self.assertFalse(conflicts)
            self.assertEqual(changes, 0)
            self.assertEqual(first, second)

    def test_conflict_rejects_entire_batch(self):
        original, _, _ = merge_profile(empty_profile(), interview_facts({"name": "가나다"}))
        batch = interview_facts({"name": "다른 이름", "technical_domain": "새 영역"})
        merged, conflicts, changes = merge_profile(original, batch)
        self.assertEqual(merged, original)
        self.assertEqual(changes, 0)
        self.assertEqual([item["field"] for item in conflicts], ["name"])
        self.assertNotIn("technical_domain", merged["facts"])

    def test_conflicting_values_inside_new_batch_are_atomic(self):
        claim = Claim(EpistemicLabel.USER_STATEMENT, source_id="interview-v1")
        batch = [IncomingFact("role", "개발자", claim), IncomingFact("role", "발명자", claim)]
        merged, conflicts, _ = merge_profile(empty_profile(), batch)
        self.assertEqual(merged, empty_profile())
        self.assertEqual(len(conflicts), 1)

    def test_folder_and_document_normalize_equivalent_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            document = folder / "facts.json"
            document.write_text(json.dumps({"name": "홍길동", "technical_domain": "산업 AI"}), encoding="utf-8")
            from_document, _, _ = merge_profile(empty_profile(), document_facts(document))
            from_folder, _, _ = merge_profile(empty_profile(), folder_facts(folder))
            self.assertEqual(
                {key: value["value"] for key, value in from_document["facts"].items()},
                {key: value["value"] for key, value in from_folder["facts"].items()},
            )
            self.assertTrue(all(entry["claims"][0]["label"] == "source_fact" for entry in from_folder["facts"].values()))

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.md"
            target.write_text("name: private", encoding="utf-8")
            link = root / "link.md"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                document_facts(link)

    def test_folder_recurses_in_deterministic_lexical_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "b.md").write_text("second: 2\n", encoding="utf-8")
            (root / "a").mkdir()
            (root / "a" / "z.md").write_text("first: 1\n", encoding="utf-8")
            (root / "a" / "ignored.bin").write_bytes(b"ignored")
            (root / "c").mkdir()
            (root / "c" / "a.txt").write_text("third: 3\n", encoding="utf-8")

            facts = folder_facts(root)

            self.assertEqual([fact.field for fact in facts], ["first", "second", "third"])
            expected_sources = [
                "src_" + __import__("hashlib").sha256(
                    json.dumps(
                        {"content_hash": fact.claim.content_hash, "locator": locator},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:16]
                for fact, locator in zip(facts, ("a/z.md", "b.md", "c/a.txt"))
            ]
            self.assertEqual([fact.claim.source_id for fact in facts], expected_sources)

    def test_nested_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            target = root / "target.md"
            target.write_text("name: private\n", encoding="utf-8")
            (nested / "link.md").symlink_to(target)

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                folder_facts(root)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO required")
    def test_folder_rejects_posix_fifo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.mkfifo(root / "pipe.md")

            with self.assertRaisesRegex(ValueError, "nonregular path"):
                folder_facts(root)

    def test_document_root_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "selected"
            root.mkdir()
            outside = parent / "outside.md"
            outside.write_text("name: private\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "outside root"):
                document_facts(root / ".." / "outside.md", root=root)


if __name__ == "__main__":
    unittest.main()
