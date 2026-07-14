import unittest

from patent_factory.corpus import build_retained_corpus
from patent_factory.models import QueryEnvelope


class CorpusTests(unittest.TestCase):
    def hit(self, index, rank=1, query="qu_1"):
        identity = f"10-2026-{index:07d}"
        return {
            "content_hash": f"hash-{index}", "evidence_id": f"ev_{index}",
            "logical_query_id": query, "query_id": query,
            "record": {"application_number": identity, "title": f"record {index}"}, "source_rank": rank,
        }

    def test_top_hundred_retains_all_substantive_boundary_ties(self):
        hits = [self.hit(index, rank=1) for index in range(103)]
        corpus = build_retained_corpus(finalist_id="fi_1", query_group_id="aq_1", hits=reversed(hits))
        self.assertEqual(corpus["retained_count"], 103)
        self.assertEqual(corpus["excluded_count"], 0)
        self.assertEqual(corpus, build_retained_corpus(finalist_id="fi_1", query_group_id="aq_1", hits=hits))

    def test_duplicate_identity_revision_accumulates_query_hits(self):
        corpus = build_retained_corpus(
            finalist_id="fi_1", query_group_id="aq_1",
            hits=[self.hit(1, 4, "qu_a"), self.hit(1, 2, "qu_b")],
        )
        self.assertEqual(corpus["records"][0]["query_hit_count"], 2)
        self.assertEqual(corpus["records"][0]["best_source_rank"], 2)

    def test_pagination_attempts_count_once_per_logical_query_but_are_preserved(self):
        first, second = self.hit(1, 4, "qu_page_1"), self.hit(1, 2, "qu_page_2")
        first["logical_query_id"] = second["logical_query_id"] = "lq_same"
        corpus = build_retained_corpus(finalist_id="fi_1", query_group_id="aq_1", hits=[first, second])
        record = corpus["records"][0]
        self.assertEqual(record["query_hit_count"], 1)
        self.assertEqual(record["logical_query_ids"], ["lq_same"])
        self.assertEqual(record["query_ids"], ["qu_page_1", "qu_page_2"])

    def test_excluded_record_material_is_hash_bound(self):
        hits = [self.hit(index, rank=index + 1) for index in range(101)]
        first = build_retained_corpus(finalist_id="fi_1", query_group_id="aq_1", hits=hits)
        changed = [dict(item) for item in hits]
        changed[-1] = {**changed[-1], "content_hash": "changed-excluded-revision", "evidence_id": "ev_changed"}
        second = build_retained_corpus(finalist_id="fi_1", query_group_id="aq_1", hits=changed)
        self.assertEqual(first["excluded_count"], 1)
        self.assertEqual(first["excluded_records"][0]["reason_code"], "below_retention_boundary")
        self.assertNotEqual(first["corpus_hash"], second["corpus_hash"])

    def test_audit_binding_separates_identical_text_without_entering_projection(self):
        base = dict(
            run_id="run", adapter="kipris", adapter_version="plus-xml-v1", capability="word_search",
            allowed_scheme="https", allowed_host="plus.kipris.or.kr", deadline_seconds=10,
            page=1, page_cap=5, result_budget=100, byte_budget=1000, retry_budget=0,
            retry_ownership="audit_runner", query_projection={"word": "same"},
        )
        first = QueryEnvelope(**base, audit_binding={
            "purpose": "final_similarity_audit", "finalist_set_hash": "a" * 64,
            "finalist_id": "fi_1", "query_group_id": "aq_1",
        })
        second = QueryEnvelope(**base, audit_binding={
            "purpose": "final_similarity_audit", "finalist_set_hash": "a" * 64,
            "finalist_id": "fi_2", "query_group_id": "aq_2",
        })
        self.assertNotEqual(first.request_fingerprint, second.request_fingerprint)
        self.assertEqual(first.query_projection, second.query_projection)


if __name__ == "__main__":
    unittest.main()
