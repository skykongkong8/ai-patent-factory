import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from patent_factory.adapters.base import TransportResponse
from patent_factory.adapters.kipris import KiprisAdapter
from patent_factory.audit import feature_map_id, run_audit_retrieval, run_audit_scoring
from patent_factory.scaffold import seal_feature_map_input
from patent_factory.config import load_evaluation_config, load_similarity_config
from patent_factory.database import connect_database
from patent_factory.evaluation import run_shortlist
from patent_factory.ideation import run_ideation
from patent_factory.models import RunState
from patent_factory.provenance import digest
from patent_factory.privacy import PrivacyError
from patent_factory.research import CredentialRequiredError
from patent_factory.similarity import score_pair as production_score_pair
from patent_factory.state import StateError, StateStore
from tests.integration.test_g004_ideation_and_shortlist import (
    candidate_input, ready_profile, ready_research, shortlist_input,
)
from tests.unit.test_g005_similarity import feature_map

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None


def kipris_xml(number):
    """A one-item KIPRIS response in the shape the live service actually returns.

    This helper drives the golden end-to-end journey (tests/e2e/test_full_journey.py).
    It used to nest <numOfRows>/<pageNo>/<totalCount> INSIDE <body> — the exact
    invented shape that let #38 ship, where the live path failed 100% of the time
    while every test passed. The live service emits <count> as a SIBLING of
    <body>; see the recorded response in tests/fixtures/kipris/word-search-live-v1.xml
    and the structural guard in tests/unit/test_kipris_live_shape.py.

    The multi-value ipcNumber below is also deliberate: the live service packs
    codes into one pipe-delimited element, which is what #39 mis-scored.
    """

    return f"""<?xml version='1.0' encoding='UTF-8'?>
<response><header><successYN>Y</successYN><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header><body><items><item>
<inventionTitle>공통 감사 기술</inventionTitle><ipcNumber>G06F 1/00|G06N 3/04</ipcNumber>
<applicationNumber>{number}</applicationNumber><applicationDate>20260101</applicationDate>
<applicantName>공개 출원인</applicantName><astrtCont>동일 메커니즘 공개 초록</astrtCont>
<registerStatus>등록</registerStatus><registerDate>20260301</registerDate>
</item></items></body><count><numOfRows>100</numOfRows><pageNo>1</pageNo><totalCount>1</totalCount></count></response>""".encode()


class G005AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect_database(self.root / "factory.sqlite3")
        self.profile_connection, self.profile = ready_profile(self.root / "profile.sqlite3")
        evidence, span, _ = ready_research(self.connection, self.root)
        ideation = run_ideation(
            self.connection, profile_connection=self.profile_connection, run_root=self.root,
            run_id="run", profile=self.profile, candidate_input=candidate_input(3, evidence, span),
            config=load_evaluation_config(),
        )
        run_shortlist(
            self.connection, run_root=self.root, run_id="run",
            shortlist_input=shortlist_input(ideation.candidate_ids, evidence, span),
            config=load_evaluation_config(),
        )

    def tearDown(self):
        self.connection.close()
        self.profile_connection.close()
        self.temporary.cleanup()

    def query_fixture(self):
        row = self.connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id='run' AND ca.kind='finalist_set'"
        ).fetchone()
        finalists = json.loads(row["content_json"])["finalists"]
        return row, finalists, {
            "schema_version": "audit-query-input-v1", "finalist_set_hash": row["content_hash"],
            "groups": [{
                "finalist_id": finalist["finalist_id"],
                "queries": [{"language": "ko", "term": "동일 검색어"}, {"language": "en", "term": "same query"}],
            } for finalist in finalists],
        }

    def test_duplicate_finalist_group_rejects_before_write(self):
        _row, _finalists, query_input = self.query_fixture()
        query_input["groups"].append(dict(query_input["groups"][0]))
        before = StateStore(self.connection).snapshot("run")
        with self.assertRaisesRegex(ValueError, "exactly one group"):
            run_audit_retrieval(
                self.connection, run_root=self.root, run_id="run", query_input=query_input,
                config=load_similarity_config(), adapter_factory=lambda query, page, finalist: None,
            )
        after = StateStore(self.connection).snapshot("run")
        self.assertEqual((before.state, before.state_version, before.current_revisions), (after.state, after.state_version, after.current_revisions))

    def test_missing_credential_suspends_before_audit_artifact_or_query_write(self):
        _row, _finalists, query_input = self.query_fixture()
        with self.assertRaises(CredentialRequiredError):
            run_audit_retrieval(
                self.connection, run_root=self.root, run_id="run", query_input=query_input,
                config=load_similarity_config(),
                adapter_factory=lambda query, page, finalist: KiprisAdapter(None),
            )
        snapshot = StateStore(self.connection).snapshot("run")
        self.assertEqual(snapshot.state, RunState.CREDENTIAL_REQUIRED)
        self.assertNotIn("audit_query_set", snapshot.current_revisions)
        self.assertNotIn("scorer_config", snapshot.current_revisions)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_queries").fetchone()[0], 0)

    def test_current_credential_decision_claims_exact_audit_request_before_egress(self):
        row, _finalists, query_input = self.query_fixture()
        with self.assertRaises(CredentialRequiredError) as captured:
            run_audit_retrieval(
                self.connection, run_root=self.root, run_id="run", query_input=query_input,
                config=load_similarity_config(), adapter_factory=lambda query, page, finalist: KiprisAdapter(None),
            )
        gate = captured.exception.gate
        decision, _ = StateStore(self.connection).decide_gate(
            gate.gate_id, action="configure_and_verify", actor="user", reason="configured",
            subject_revision_hash=gate.subject_revision_hash, approval_scope=gate.approval_scope,
        )
        with self.assertRaisesRegex(StateError, "remains unavailable"):
            run_audit_retrieval(
                self.connection, run_root=self.root, run_id="run", query_input=query_input,
                config=load_similarity_config(), adapter_factory=lambda query, page, finalist: KiprisAdapter(None),
                credential_decision_id=decision.decision_id,
            )
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_queries").fetchone()[0], 0)
        self.assertEqual(tuple(self.connection.execute(
            "SELECT consumed_at,used_at FROM gate_decisions WHERE decision_id=?", (decision.decision_id,),
        ).fetchone()), (None, None))

        calls = []
        body = kipris_xml("10-2026-0012345")
        result = run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run", query_input=query_input,
            config=load_similarity_config(),
            adapter_factory=lambda query, page, finalist: KiprisAdapter(
                "configured", transport=lambda *_: calls.append((finalist, page)) or TransportResponse(200, {}, body),
            ),
            credential_decision_id=decision.decision_id,
        )
        self.assertEqual(result.run_id, "run")
        self.assertTrue(calls)
        claimed = self.connection.execute(
            "SELECT used_at,consumed_by_event_id FROM gate_decisions WHERE decision_id=?",
            (decision.decision_id,),
        ).fetchone()
        self.assertTrue(claimed["used_at"] and claimed["consumed_by_event_id"])
        self.assertEqual(row["content_hash"], gate.subject_revision_hash)

    def test_response_credential_canary_is_rejected_before_research_persistence(self):
        _row, _finalists, query_input = self.query_fixture()
        secret = "G005-RESPONSE-CREDENTIAL-CANARY"
        body = kipris_xml("10-2026-0012345").replace("공통 감사 기술".encode(), secret.encode())
        factory = lambda query, page, finalist: KiprisAdapter(
            "fixture", credential_required=False,
            transport=lambda url, timeout, byte_budget: TransportResponse(200, {}, body),
        )
        with patch.dict("os.environ", {"KIPRIS_PLUS_API_KEY": secret}):
            with self.assertRaisesRegex(PrivacyError, "adapter_response: canary_detected") as captured:
                run_audit_retrieval(
                    self.connection, run_root=self.root, run_id="run", query_input=query_input,
                    config=load_similarity_config(), adapter_factory=factory,
                )
        self.assertNotIn(secret, str(captured.exception))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_queries").fetchone()[0], 0)
        self.assertNotIn(secret.encode(), (self.root / "factory.sqlite3").read_bytes())

    def test_config_and_finalist_invalidation_rebuilds_current_audit_retrieval(self):
        finalist_row, _finalists, query_input = self.query_fixture()
        body = kipris_xml("10-2026-0012345")
        factory = lambda query, page, finalist: KiprisAdapter(
            "fixture", credential_required=False,
            transport=lambda url, timeout, byte_budget: TransportResponse(200, {}, body),
        )
        run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run", query_input=query_input,
            config=load_similarity_config(), adapter_factory=factory,
        )
        store = StateStore(self.connection)
        drift = store.add_revision("run", "scorer_config", {"version": "drift"}, schema_version="drift")
        self.assertNotIn("audit_query_set", store.snapshot("run").current_revisions)
        refreshed = run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run", query_input=query_input,
            config=load_similarity_config(), adapter_factory=factory,
        )
        self.assertIn("corpus_set", store.snapshot("run").current_revisions)
        self.assertNotEqual(store.snapshot("run").current_revisions["scorer_config"], drift.revision_id)

        finalist_content = json.loads(finalist_row["content_json"])
        finalist_content["refresh_marker"] = "new-current-finalist-revision"
        new_finalist = store.add_revision(
            "run", "finalist_set", finalist_content, schema_version="finalist-set-v1",
        )
        query_input["finalist_set_hash"] = new_finalist.content_hash
        rerun = run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run", query_input=query_input,
            config=load_similarity_config(), adapter_factory=factory,
        )
        snapshot = store.snapshot("run")
        self.assertIn("audit_query_set", snapshot.current_revisions)
        self.assertIn("corpus_set", snapshot.current_revisions)
        self.assertNotEqual(refreshed.query_set_revision_id, rerun.query_set_revision_id)

    def test_identical_terms_make_separate_post_shortlist_groups_and_atomic_excessive_gate(self):
        finalist_row, finalists, query_input = self.query_fixture()

        def factory(query, page, finalist_id):
            self.assertEqual(page, 1)
            number = "10-2026-" + str(int(finalist_id[-4:], 16) % 10_000_000).zfill(7)
            body = kipris_xml(number)
            return KiprisAdapter(
                "fixture", credential_required=False,
                transport=lambda url, timeout, byte_budget: TransportResponse(200, {}, body),
            )

        retrieval = run_audit_retrieval(
            self.connection, run_root=self.root, run_id="run", query_input=query_input,
            config=load_similarity_config(), adapter_factory=factory,
        )
        self.assertEqual(len(retrieval.corpus_hashes), 3)
        query_rows = self.connection.execute(
            "SELECT envelope_json FROM research_queries WHERE run_id='run' AND envelope_json LIKE '%final_similarity_audit%'"
        ).fetchall()
        self.assertEqual(len(query_rows), 6)
        bindings = [json.loads(row["envelope_json"])["audit_binding"] for row in query_rows]
        self.assertEqual(len({item["finalist_id"] for item in bindings}), 3)

        corpus_row = self.connection.execute(
            "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id='run' AND ca.kind='corpus_set'"
        ).fetchone()
        corpus_set = json.loads(corpus_row["content_json"])
        candidate_row = self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca ON ca.revision_id=ar.revision_id "
            "WHERE ca.run_id='run' AND ca.kind='candidate_set'"
        ).fetchone()
        candidates = {item["candidate_id"]: item for item in json.loads(candidate_row["content_json"])["candidates"]}
        finalist_by_id = {item["finalist_id"]: item for item in finalists}
        maps = []
        for corpus in corpus_set["corpora"]:
            record = corpus["records"][0]
            mapping = feature_map(record["evidence_id"])
            candidate = candidates[finalist_by_id[corpus["finalist_id"]]["candidate_id"]]
            fields = {
                "problem": "technical_problem", "inputs": "required_inputs", "mechanism": "mechanism",
                "transformations": "transformations", "outputs": "outputs", "technical_effects": "expected_effects",
            }
            for feature in mapping["features"]:
                field = fields[feature["category"]]
                raw = candidate[field]
                value = raw[0] if isinstance(raw, list) else raw
                feature["candidate_span_hashes"] = [digest({"field": field, "text": value})]
            span = record["record"]["field_span_hashes"]["abstract"]
            for decision in mapping["reference_maps"][0]["decisions"]:
                decision["reference_span_hashes"] = [span]
            maps.append({
                "feature_map": mapping, "finalist_id": corpus["finalist_id"],
                "map_id": feature_map_id(corpus["finalist_id"], mapping),
            })
        before_bad_maps = StateStore(self.connection).snapshot("run")
        duplicate_map = json.loads(json.dumps(maps)) + [json.loads(json.dumps(maps[0]))]
        with self.assertRaisesRegex(ValueError, "exactly one frozen map"):
            run_audit_scoring(
                self.connection, run_root=self.root, run_id="run",
                feature_input={
                    "schema_version": "feature-map-set-input-v1", "finalist_set_hash": finalist_row["content_hash"],
                    "corpus_set_hash": corpus_row["content_hash"], "maps": duplicate_map,
                }, config=load_similarity_config(),
            )
        duplicate_id = json.loads(json.dumps(maps))
        duplicate_id[1]["map_id"] = duplicate_id[0]["map_id"]
        with self.assertRaisesRegex(ValueError, "duplicate finalist or map identity"):
            run_audit_scoring(
                self.connection, run_root=self.root, run_id="run",
                feature_input={
                    "schema_version": "feature-map-set-input-v1", "finalist_set_hash": finalist_row["content_hash"],
                    "corpus_set_hash": corpus_row["content_hash"], "maps": duplicate_id,
                }, config=load_similarity_config(),
            )
        after_bad_maps = StateStore(self.connection).snapshot("run")
        self.assertEqual(
            (before_bad_maps.state_version, before_bad_maps.current_revisions),
            (after_bad_maps.state_version, after_bad_maps.current_revisions),
        )
        duplicate_decision = json.loads(json.dumps(maps))
        conflict = dict(duplicate_decision[0]["feature_map"]["reference_maps"][0]["decisions"][0])
        conflict["status"] = "different"
        duplicate_decision[0]["feature_map"]["reference_maps"][0]["decisions"].append(conflict)
        before_decision = StateStore(self.connection).snapshot("run")
        with self.assertRaisesRegex(ValueError, "duplicate or empty decision identity"):
            run_audit_scoring(
                self.connection, run_root=self.root, run_id="run",
                feature_input={
                    "schema_version": "feature-map-set-input-v1", "finalist_set_hash": finalist_row["content_hash"],
                    "corpus_set_hash": corpus_row["content_hash"], "maps": duplicate_decision,
                }, config=load_similarity_config(),
            )
        after_decision = StateStore(self.connection).snapshot("run")
        self.assertEqual(
            (before_decision.state_version, before_decision.current_revisions),
            (after_decision.state_version, after_decision.current_revisions),
        )
        empty_rationale = json.loads(json.dumps(maps))
        empty_rationale[0]["feature_map"]["reference_maps"][0]["decisions"][0]["rationale"] = "  "
        empty_rationale[0]["map_id"] = feature_map_id(
            empty_rationale[0]["finalist_id"], empty_rationale[0]["feature_map"]
        )
        before_rationale = StateStore(self.connection).snapshot("run")
        with self.assertRaisesRegex(ValueError, "nonempty rationale"):
            run_audit_scoring(
                self.connection, run_root=self.root, run_id="run",
                feature_input={
                    "schema_version": "feature-map-set-input-v1", "finalist_set_hash": finalist_row["content_hash"],
                    "corpus_set_hash": corpus_row["content_hash"], "maps": empty_rationale,
                }, config=load_similarity_config(),
            )
        after_rationale = StateStore(self.connection).snapshot("run")
        self.assertEqual(
            (before_rationale.state_version, before_rationale.current_revisions),
            (after_rationale.state_version, after_rationale.current_revisions),
        )
        bad_fields = json.loads(json.dumps(maps))
        bad_fields[0]["feature_map"]["reference_maps"][0]["inspected_fields"] = ["nonexistent_field"]
        bad_fields[0]["map_id"] = feature_map_id(bad_fields[0]["finalist_id"], bad_fields[0]["feature_map"])
        with self.assertRaisesRegex(ValueError, "real retained evidence field"):
            run_audit_scoring(
                self.connection, run_root=self.root, run_id="run",
                feature_input={
                    "schema_version": "feature-map-set-input-v1", "finalist_set_hash": finalist_row["content_hash"],
                    "corpus_set_hash": corpus_row["content_hash"], "maps": bad_fields,
                }, config=load_similarity_config(),
            )
        bad_candidate = json.loads(json.dumps(maps))
        bad_candidate[0]["feature_map"]["features"][0]["candidate_span_hashes"] = [
            digest({"field": "candidate_id", "text": bad_candidate[0]["finalist_id"]})
        ]
        bad_candidate[0]["map_id"] = feature_map_id(bad_candidate[0]["finalist_id"], bad_candidate[0]["feature_map"])
        with self.assertRaisesRegex(ValueError, "candidate span"):
            run_audit_scoring(
                self.connection, run_root=self.root, run_id="run",
                feature_input={
                    "schema_version": "feature-map-set-input-v1", "finalist_set_hash": finalist_row["content_hash"],
                    "corpus_set_hash": corpus_row["content_hash"], "maps": bad_candidate,
                }, config=load_similarity_config(),
            )
        for exact_field in ("T", "F", "C", "D", "Q", "r_obs", "r_hi"):
            for inconsistent in (
                {"numerator": 101, "denominator": 1, "value": 100},
                {"numerator": 1, "denominator": 3, "value": 0.34},
            ):
                before_exact = StateStore(self.connection).snapshot("run")

                def corrupt_score(*args, _field=exact_field, _value=inconsistent, **kwargs):
                    score = production_score_pair(*args, **kwargs)
                    score[f"{_field}_exact"] = dict(_value)
                    return score

                with self.subTest(exact_field=exact_field, inconsistent=inconsistent):
                    with patch("patent_factory.audit.score_pair", side_effect=corrupt_score):
                        with self.assertRaisesRegex(ValueError, "inconsistent|display"):
                            run_audit_scoring(
                                self.connection, run_root=self.root, run_id="run",
                                feature_input={
                                    "schema_version": "feature-map-set-input-v1",
                                    "finalist_set_hash": finalist_row["content_hash"],
                                    "corpus_set_hash": corpus_row["content_hash"], "maps": maps,
                                }, config=load_similarity_config(),
                            )
                    after_exact = StateStore(self.connection).snapshot("run")
                    self.assertEqual(
                        (before_exact.state_version, before_exact.current_revisions),
                        (after_exact.state_version, after_exact.current_revisions),
                    )
        # An agent driving the CLI cannot compute map_id: it digests the *filled*
        # map, so it only exists after the judgment fields are written. Prove the
        # seal derives exactly what run_audit_scoring demands, starting from input
        # whose map_id values are deliberately wrong.
        unsealed = json.loads(json.dumps({
            "schema_version": "feature-map-set-input-v1",
            "finalist_set_hash": finalist_row["content_hash"],
            "corpus_set_hash": corpus_row["content_hash"],
            # Distinct stale ids, so this reaches the identity-binding check rather
            # than tripping the duplicate-map_id check first.
            "maps": [
                {**item, "map_id": f"fm_stale{index:013d}"} for index, item in enumerate(maps)
            ],
        }))
        with self.assertRaisesRegex(ValueError, "map identity does not bind frozen content"):
            run_audit_scoring(
                self.connection, run_root=self.root, run_id="run",
                feature_input=unsealed, config=load_similarity_config(),
            )
        sealed = seal_feature_map_input(unsealed)
        self.assertEqual(
            [item["map_id"] for item in sealed["maps"]],
            [feature_map_id(item["finalist_id"], item["feature_map"]) for item in maps],
        )
        # Sealing re-derives identity only; it must not touch a judgment field.
        self.assertEqual(
            [item["feature_map"] for item in sealed["maps"]],
            [item["feature_map"] for item in unsealed["maps"]],
        )
        scored = run_audit_scoring(
            self.connection, run_root=self.root, run_id="run",
            feature_input=sealed,
            config=load_similarity_config(),
        )
        self.assertEqual(scored.state, RunState.DECISION_REQUIRED.value)
        self.assertIsNotNone(scored.gate_id)
        gate = self.connection.execute("SELECT * FROM gate_envelopes WHERE gate_id=?", (scored.gate_id,)).fetchone()
        audit = self.connection.execute("SELECT content_hash FROM artifact_revisions WHERE revision_id=?", (scored.artifact_revision_id,)).fetchone()
        self.assertEqual(gate["subject_revision_hash"], audit["content_hash"])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM gate_envelopes WHERE status='pending'").fetchone()[0], 1)
        feature_export = json.loads(self.connection.execute(
            "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
            "ON ca.revision_id=ar.revision_id WHERE ca.run_id='run' AND ca.kind='feature_map_set'"
        ).fetchone()["content_json"])
        audit_export = json.loads(self.connection.execute(
            "SELECT content_json FROM artifact_revisions WHERE revision_id=?", (scored.artifact_revision_id,)
        ).fetchone()["content_json"])
        for wrapper in feature_export["maps"]:
            self.assertIsInstance(wrapper["feature_map"]["features"], dict)
            self.assertTrue(wrapper["feature_map"]["features"])
            for reference_map in wrapper["feature_map"]["reference_maps"]:
                self.assertIsInstance(reference_map["decisions"], dict)
        if Draft202012Validator is not None:
            repository = Path(__file__).resolve().parents[2]
            Draft202012Validator(json.loads(
                (repository / "schemas/feature-map.schema.json").read_text(encoding="utf-8")
            )).validate(feature_export)
            Draft202012Validator(json.loads(
                (repository / "schemas/audit.schema.json").read_text(encoding="utf-8")
            )).validate(audit_export)
        replay = run_audit_scoring(
            self.connection, run_root=self.root, run_id="run",
            feature_input={
                "schema_version": "feature-map-set-input-v1", "finalist_set_hash": finalist_row["content_hash"],
                "corpus_set_hash": corpus_row["content_hash"], "maps": maps,
            }, config=load_similarity_config(),
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.artifact_revision_id, scored.artifact_revision_id)
        drift = json.loads(json.dumps(maps))
        drift[0]["feature_map"]["review"]["reviewed_by"] = "different-reviewer"
        drift[0]["map_id"] = feature_map_id(drift[0]["finalist_id"], drift[0]["feature_map"])
        with self.assertRaisesRegex(StateError, "does not match"):
            run_audit_scoring(
                self.connection, run_root=self.root, run_id="run",
                feature_input={
                    "schema_version": "feature-map-set-input-v1", "finalist_set_hash": finalist_row["content_hash"],
                    "corpus_set_hash": corpus_row["content_hash"], "maps": drift,
                }, config=load_similarity_config(),
            )


if __name__ == "__main__":
    unittest.main()
