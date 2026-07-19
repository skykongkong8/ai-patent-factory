import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from patent_factory import cli
from patent_factory.adapters.manual_web import normalize_web_rows, sanitize_manual_records
from patent_factory.database import connect_database
from patent_factory.provenance import digest
from tests.integration.test_g009_research_batch import ready

ROOT = Path(__file__).resolve().parents[2]


def rows_payload():
    return {
        "schema_version": "web-rows-v1",
        "rows": [
            {
                "url": "https://arxiv.org/abs/2501.01234",
                "title": "Adaptive KV-Cache Compression for On-Device LLM Inference",
                "identifier": "arXiv:2501.01234",
                "abstract": "We present an attention-entropy-driven compression scheme.",
                "excerpts": ["Entropy-guided precision control reduces cache memory."],
                "limitations": ["arXiv preprint metadata only"],
                "language": "en",
            },
            {
                "url": "https://patents.google.com/patent/KR102026000001",
                "title": "모바일 NPU용 KV-캐시 관리 장치",
                "identifier": "KR-10-2026-0000001",
                "language": "ko",
            },
        ],
    }


class NormalizeWebRowsTests(unittest.TestCase):
    def test_rows_normalize_with_kipris_style_span_hashes_and_round_trip(self):
        records = normalize_web_rows(
            rows_payload()["rows"], ("arxiv.org", "patents.google.com"), "arxiv",
        )
        self.assertEqual(len(records), 2)
        first = records[0]
        title_hash = digest({"field": "title", "text": rows_payload()["rows"][0]["title"]})
        abstract_hash = digest({"field": "abstract", "text": rows_payload()["rows"][0]["abstract"]})
        excerpt_hash = digest({"field": "excerpt_01", "text": rows_payload()["rows"][0]["excerpts"][0]})
        self.assertEqual(first["excerpt_hashes"], sorted([title_hash, abstract_hash, excerpt_hash]))
        self.assertEqual(first["provenance"], "arxiv")
        self.assertEqual(first["canonical_url"], "https://arxiv.org/abs/2501.01234")
        # Emitted records are import-ready by construction.
        self.assertEqual(sanitize_manual_records(records, ("arxiv.org", "patents.google.com")), records)

    def test_rejections(self):
        base = rows_payload()["rows"][0]
        with self.assertRaises(PermissionError):
            normalize_web_rows([{**base, "url": "http://arxiv.org/abs/1"}], ("arxiv.org",), "arxiv")
        with self.assertRaises(PermissionError):
            normalize_web_rows([base], ("naver.com",), "arxiv")
        with self.assertRaises(ValueError):
            normalize_web_rows([{**base, "identifier": ""}], ("arxiv.org",), "arxiv")
        with self.assertRaises(ValueError):
            normalize_web_rows([{**base, "raw_document": "PRIVATE"}], ("arxiv.org",), "arxiv")
        with self.assertRaises(ValueError):
            normalize_web_rows([base], ("arxiv.org",), "not-a-tag")
        with self.assertRaises(ValueError):
            normalize_web_rows([], ("arxiv.org",), "arxiv")


class NormalizeWebCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "workspace")
        base = Path(self.temporary.name)
        self.documents = base / "documents"
        self.documents.mkdir(mode=0o700)
        self.workspace = base / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.documents_rel = self.documents.relative_to(ROOT)
        self.workspace_rel = self.workspace.relative_to(ROOT)
        (self.documents / "web-rows.json").write_text(
            json.dumps(rows_payload(), ensure_ascii=False), encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli.main([str(item) for item in argv])
        return json.loads(stream.getvalue()), code

    def test_normalize_then_manual_import_end_to_end(self):
        payload, code = self.invoke(
            "research", "normalize-web", self.documents_rel / "web-rows.json",
            "--out", self.documents_rel / "normalized.json",
            "--allow-host", "arxiv.org", "--allow-host", "patents.google.com",
            "--source-type", "arxiv",
            "--documents-root", self.documents_rel, "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "normalized")
        self.assertEqual(payload["record_count"], 2)
        self.assertEqual(payload["source_type"], "arxiv")
        written = json.loads((self.documents / "normalized.json").read_text(encoding="utf-8"))
        self.assertEqual(len(written["records"]), 2)

        run_root = self.workspace / "run"
        run_root.mkdir(mode=0o700)
        connection = connect_database(run_root / "factory.sqlite3")
        try:
            ready(connection)
        finally:
            connection.close()
        imported, code = self.invoke(
            "research", "manual", self.documents_rel / "normalized.json",
            "--run", run_root.relative_to(ROOT), "--run-id", "run",
            "--query", "on-device kv-cache", "--allow-host", "arxiv.org",
            "--allow-host", "patents.google.com",
            "--retrieved-at", "2026-07-19T00:00:00Z",
            "--documents-root", self.documents_rel, "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 0, imported)
        self.assertEqual(imported["status"], "complete")
        self.assertEqual(imported["evidence_count"], 2)
        connection = connect_database(run_root / "factory.sqlite3")
        try:
            rows = connection.execute(
                "SELECT source_type, provenance FROM evidence_records ORDER BY evidence_id",
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual({row["source_type"] for row in rows}, {"manual_web"})
        self.assertEqual({row["provenance"] for row in rows}, {"arxiv"})

    def test_malformed_envelope_is_rejected(self):
        (self.documents / "bad.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
        payload, code = self.invoke(
            "research", "normalize-web", self.documents_rel / "bad.json",
            "--out", self.documents_rel / "never.json", "--allow-host", "arxiv.org",
            "--documents-root", self.documents_rel, "--workspace-root", self.workspace_rel,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "error")
        self.assertFalse((self.documents / "never.json").exists())


if __name__ == "__main__":
    unittest.main()
