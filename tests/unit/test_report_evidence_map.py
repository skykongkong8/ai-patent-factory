import tempfile
import unittest
from pathlib import Path

from patent_factory.database import connect_database
from patent_factory.report import _evidence_map
from patent_factory.state import StateStore

ROOT = Path(__file__).resolve().parents[2]

RESEARCH = {
    "evidence": [{
        "canonical_url": "https://plus.kipris.or.kr/record/1", "content_hash": "a" * 64,
        "created_at": "2026-07-15T00:00:00Z", "evidence_id": "ev_0000000000000001",
        "original_identifier": "10-2026-0000001",
        "record_json": '{"limitations": ["정적 압축 한정"]}',
        "source_type": "kipris_patent", "title": "선행기술 1",
    }],
}
CORPUS = {
    "corpora": [{
        "records": [
            {
                "application_identity": "1020260000001", "content_hash": "a" * 64,
                "evidence_id": "ev_0000000000000001",
                "record": {"application_number": "10-2026-0000001", "title": "선행기술 1"},
            },
            {
                "application_identity": "1020260000002", "content_hash": "b" * 64,
                "evidence_id": "ev_0000000000000002",
                "record": {"application_number": "10-2026-0000002", "title": "감사 전용 문헌"},
            },
        ],
    }],
}


class EvidenceMapProvenanceTests(unittest.TestCase):
    def test_corpus_projection_never_overwrites_research_bundle_provenance(self):
        result = _evidence_map(RESEARCH, CORPUS)
        shared = result["ev_0000000000000001"]
        self.assertEqual(shared["source_type"], "kipris_patent")
        self.assertEqual(shared["observation_date"], "2026-07-15")
        self.assertEqual(shared["limitations"], ["정적 압축 한정"])
        corpus_only = result["ev_0000000000000002"]
        self.assertEqual(corpus_only["source_type"], "kipris_audit")
        self.assertEqual(corpus_only["identifier"], "10-2026-0000002")

    def test_evidence_records_remain_the_authoritative_last_writer(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "workspace") as directory:
            connection = connect_database(Path(directory) / "factory.sqlite3")
            try:
                StateStore(connection).create_run("run")
                connection.execute(
                    "INSERT INTO evidence_records(run_id,evidence_id,source_type,source_locator,"
                    "original_identifier,title,canonical_url,content_hash,language,record_json,"
                    "created_at,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("run", "ev_0000000000000001", "kipris_patent",
                     "https://plus.kipris.or.kr/record/1", "10-2026-0000001",
                     "선행기술 1", "https://plus.kipris.or.kr/record/1", "a" * 64,
                     "ko", '{"limitations": ["DB 기록"]}', "2026-07-14T00:00:00Z", "fixture"),
                )
                result = _evidence_map(RESEARCH, CORPUS, connection=connection, run_id="run")
            finally:
                connection.close()
        authoritative = result["ev_0000000000000001"]
        self.assertEqual(authoritative["source_type"], "kipris_patent")
        self.assertEqual(authoritative["observation_date"], "2026-07-14")
        self.assertEqual(authoritative["limitations"], ["DB 기록"])


if __name__ == "__main__":
    unittest.main()
