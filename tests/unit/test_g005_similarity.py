import unittest
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from patent_factory.audit import validate_audit_artifact
from patent_factory.config import load_similarity_config
from patent_factory.provenance import normalize, strict_json_loads
from patent_factory.similarity import (
    canonical_feature_map,
    candidate_outcome,
    risk_label,
    score_pair,
    summarize_candidate,
    text_overlap,
    validate_feature_map,
)

try:
    from jsonschema import Draft202012Validator, ValidationError
except ImportError:  # The package remains optional for stdlib-only installations.
    Draft202012Validator = None
    ValidationError = Exception


def feature_map(evidence_id="ev_1", status="matched"):
    weights = {
        "problem": "0.10", "inputs": "0.10", "mechanism": "0.30",
        "transformations": "0.20", "outputs": "0.10", "technical_effects": "0.20",
    }
    features = [
        {"candidate_span_hashes": [f"candidate-{name}"], "category": name, "essential": True,
         "feature_id": f"feature-{name}", "weight": weight}
        for name, weight in weights.items()
    ]
    return {
        "candidate_classifications": ["G06F 1/00"], "features": features,
        "reference_maps": [{
            "decisions": [{
                "feature_id": feature["feature_id"], "rationale": "reviewed fixture",
                "reference_span_hashes": ["span"] if status in {"matched", "different", "not_disclosed"} else [],
                "status": status,
            } for feature in features],
            "evidence_id": evidence_id, "inspected_fields": ["title", "abstract", "classifications"],
        }],
        "review": {"reviewed_at": "2026-07-13T00:00:00Z", "reviewed_by": "fixture-reviewer", "status": "reviewed"},
    }


def exact_percent(value):
    value = Fraction(value)
    display = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return {
        "denominator": value.denominator,
        "numerator": value.numerator,
        "value": int(display) if "." not in display else float(display),
    }


def routing_score(evidence_id, r_obs, r_hi, coverage):
    exact = {name: exact_percent(0) for name in ("T", "F", "C", "D")}
    return {
        "T": "0", "T_exact": exact["T"],
        "F": "0", "F_exact": exact["F"],
        "C": "0", "C_exact": exact["C"],
        "D": "0", "D_exact": exact["D"],
        "Q": str(coverage), "Q_exact": exact_percent(coverage),
        "differentiated_feature_ids": [], "evidence_id": evidence_id, "label": "low",
        "matched_feature_ids": [], "r_hi": str(r_hi), "r_hi_exact": exact_percent(r_hi),
        "r_obs": str(r_obs), "r_obs_exact": exact_percent(r_obs), "version": "simrisk-v1.0.0",
    }


class SimriskTests(unittest.TestCase):
    def setUp(self):
        self.config = load_similarity_config()

    def test_exact_duplicate_is_excessive_and_exact(self):
        candidate = {"title": "adaptive sensor controller", "abstract": "calibrates sensor output"}
        reference = {**candidate, "classifications": ["G06F 1/00"], "evidence_id": "ev_1"}
        score = score_pair(candidate, reference, feature_map(), self.config)
        self.assertEqual((score["T"], score["F"], score["C"], score["D"]), ("100", "100", "100", "0"))
        self.assertEqual((score["r_obs"], score["r_hi"], score["Q"], score["label"]), ("100", "100", "100", "excessive"))
        self.assertEqual(candidate_outcome([score], self.config), "decision_required")

    def test_title_only_overlap_is_capped_at_six_point_two_five_without_features(self):
        mapping = feature_map(status="different")
        candidate = {"title": "same title", "abstract": None}
        reference = {"title": "same title", "abstract": None, "classifications": ["H01L 1/00"], "evidence_id": "ev_1"}
        score = score_pair(candidate, reference, mapping, self.config)
        self.assertEqual(score["T"], "25")
        self.assertEqual(score["r_obs"], "0")  # evidenced essential differences earn the full credit

    def test_missing_abstract_and_classification_raise_upper_bound_and_reduce_coverage(self):
        candidate = {"title": "same title", "abstract": None}
        reference = {"title": "same title", "abstract": None, "classifications": [], "evidence_id": "ev_1"}
        mapping = feature_map()
        mapping["candidate_classifications"] = []
        score = score_pair(candidate, reference, mapping, self.config)
        self.assertEqual((score["r_obs"], score["r_hi"], score["Q"]), ("66.25", "100", "66.25"))
        self.assertEqual(candidate_outcome([score], self.config), "coverage_insufficient")

    def test_bilingual_normalization_is_deterministic(self):
        self.assertEqual(text_overlap("센서 Calibration", "센서 calibration"), 1)

    def test_coverage_uses_maximum_upper_bound_pair_not_observed_argmax(self):
        scores = [
            routing_score("observed-max", "50", "60", "100"),
            routing_score("upper-max", "40", "70", "50"),
        ]
        self.assertEqual(candidate_outcome(scores, self.config), "coverage_insufficient")
        summary = summarize_candidate(scores, self.config)
        self.assertEqual(summary["observed_reference_id"], "observed-max")
        self.assertEqual(summary["upper_bound_reference_id"], "upper-max")
        self.assertEqual(summary["coverage"], "50")

    def test_exact_fraction_not_six_decimal_display_controls_threshold(self):
        below = Fraction(749999996, 10000000)
        score = routing_score("ref", "75", "75", "100")
        score["r_obs_exact"] = exact_percent(below)
        score["r_hi_exact"] = exact_percent(below)
        scores = [score]
        self.assertEqual(candidate_outcome(scores, self.config), "audit_approved")

    def test_exact_percent_runtime_rejects_inconsistent_or_out_of_range_rational(self):
        for exact in (
            {"numerator": 101, "denominator": 1, "value": 101},
            {"numerator": 101, "denominator": 1, "value": 100},
            {"numerator": -1, "denominator": 1, "value": 0},
        ):
            with self.subTest(exact=exact), self.assertRaisesRegex(ValueError, "percentage"):
                candidate_outcome([{
                    **routing_score("ref", "100", "100", "100"),
                    "r_obs_exact": exact,
                }], self.config)

    def test_legacy_list_normalization_rejects_duplicate_ids_before_keyed_canonicalization(self):
        duplicate_feature = feature_map()
        duplicate_feature["features"].append({**duplicate_feature["features"][0], "weight": "0"})
        with self.assertRaisesRegex(ValueError, "duplicate or empty feature identity"):
            canonical_feature_map(duplicate_feature)

        duplicate_decision = feature_map()
        duplicate_decision["reference_maps"][0]["decisions"].append({
            **duplicate_decision["reference_maps"][0]["decisions"][0],
            "status": "different",
        })
        with self.assertRaisesRegex(ValueError, "duplicate or empty decision identity"):
            canonical_feature_map(duplicate_decision)

        canonical = canonical_feature_map(feature_map())
        self.assertIsInstance(canonical["features"], dict)
        self.assertTrue(all(isinstance(item["decisions"], dict) for item in canonical["reference_maps"]))

    def test_same_version_rejects_any_canonical_parameter_drift(self):
        with self.assertRaisesRegex(ValueError, "version bump required"):
            replace(self.config, thresholds={**self.config.thresholds, "excessive": "75.01"}).validate()
        with self.assertRaisesRegex(ValueError, "canonical parameters"):
            replace(self.config, tokenizer={**self.config.tokenizer, "char_ngram": 4}).validate()

    def test_exact_threshold_boundary_matrix(self):
        cases = {
            "34.99": "low", "35": "moderate", "54.99": "moderate",
            "55": "high", "74.99": "high", "75": "excessive",
        }
        self.assertEqual({value: risk_label(Fraction(value), self.config) for value in cases}, cases)

    def test_frozen_goldens_execute_canonical_inputs_through_production_scorer(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/similarity/goldens/simrisk-v1.0.0.json"
        golden = strict_json_loads(path.read_text(encoding="utf-8"))
        expected_order = [
            "exact_duplicate", "paraphrased_same_mechanism", "title_only_overlap",
            "same_problem_different_mechanism", "generic_mechanism_different_context",
            "explicit_essential_difference", "missing_abstract_ipc", "bilingual_synonyms",
            "threshold_boundaries", "multiple_references",
        ]
        self.assertEqual(golden["version"], self.config.version)
        self.assertEqual([case["name"] for case in golden["cases"]], expected_order)
        pair_fields = ("T", "F", "C", "D", "r_obs", "r_hi", "Q", "label")
        for raw_case in golden["cases"]:
            case = normalize(raw_case)
            self.assertEqual(
                [item["reviewed_map"]["evidence_id"] for item in case["references"]],
                sorted(item["reviewed_map"]["evidence_id"] for item in case["references"]),
                case["name"],
            )
            candidate = case["candidate"]
            mapping = canonical_feature_map({
                "candidate_classifications": candidate["classifications"],
                "features": candidate["features"],
                "reference_maps": [item["reviewed_map"] for item in case["references"]],
                "review": case["review"],
            })
            validate_feature_map(mapping, self.config)
            scores = []
            for item in case["references"]:
                reference = {**item["record"], "evidence_id": item["reviewed_map"]["evidence_id"]}
                score = score_pair(candidate, reference, mapping, self.config)
                expected_pair = case["expected"]["pairs"][score["evidence_id"]]
                self.assertEqual(
                    {name: score[name] for name in pair_fields},
                    {name: expected_pair[name] for name in pair_fields},
                    case["name"],
                )
                exact_sources = {
                    "T_exact": "T_exact", "F_exact": "F_exact", "C_exact": "C_exact", "D_exact": "D_exact",
                    "Q_exact": "Q_exact", "r_obs_exact": "r_obs_exact", "r_hi_exact": "r_hi_exact",
                }
                self.assertEqual(
                    {name: score[name] for name in exact_sources},
                    {name: expected_pair[source] for name, source in exact_sources.items()},
                    case["name"],
                )
                scores.append(score)
            summary = summarize_candidate(scores, self.config)
            expected_summary = case["expected"]["summary"]
            self.assertEqual(summary, {
                "coverage": expected_summary["Q"],
                "observed_reference_id": expected_summary["observed_reference_id"],
                "outcome": expected_summary["route"],
                "r_hi": expected_summary["r_hi"],
                "r_obs": expected_summary["r_obs"],
                "upper_bound_reference_id": expected_summary["upper_bound_reference_id"],
            }, case["name"])
            self.assertEqual(candidate_outcome(scores, self.config), expected_summary["route"], case["name"])
            result = {
                "candidate_id": "ca_" + "1" * 20,
                "closest_reference_id": summary["observed_reference_id"],
                "corpus_hash": "a" * 64,
                "counterargument": "golden correctness fixture",
                "coverage": summary["coverage"],
                "finalist_id": "fi_" + "1" * 20,
                "outcome": summary["outcome"],
                "pair_scores": scores,
                "r_hi": summary["r_hi"],
                "r_obs": summary["r_obs"],
                "upper_bound_reference_id": summary["upper_bound_reference_id"],
            }
            validate_audit_artifact({
                "corpus_set_hash": "a" * 64,
                "feature_map_set_hash": "b" * 64,
                "finalist_set_hash": "c" * 64,
                "results": [
                    {**deepcopy(result), "candidate_id": "ca_" + str(index) * 20,
                     "finalist_id": "fi_" + str(index) * 20}
                    for index in range(1, 4)
                ],
                "run_id": "golden-validation",
                "scorer_config_hash": "d" * 64,
                "version": "audit-batch-v1",
            }, self.config)

    def test_documented_audit_and_feature_map_required_fields_match_runtime_contract(self):
        root = Path(__file__).resolve().parents[2]
        audit = strict_json_loads((root / "schemas/audit.schema.json").read_text(encoding="utf-8"))
        feature = strict_json_loads((root / "schemas/feature-map.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(audit["required"]), {
            "corpus_set_hash", "feature_map_set_hash", "finalist_set_hash", "results",
            "run_id", "scorer_config_hash", "version",
        })
        self.assertEqual(set(feature["required"]), {
            "corpus_set_hash", "finalist_set_hash", "maps", "run_id", "version",
        })
        self.assertEqual(set(feature["$defs"]["reviewedFeatureMap"]["required"]), {
            "candidate_classifications", "features", "reference_maps", "review",
        })
        self.assertEqual(set(feature["$defs"]["feature"]["required"]), {
            "candidate_span_hashes", "category", "essential", "weight",
        })
        self.assertEqual(set(audit["$defs"]["result"]["required"]), {
            "candidate_id", "closest_reference_id", "corpus_hash", "counterargument", "coverage",
            "finalist_id", "outcome", "pair_scores", "r_hi", "r_obs", "upper_bound_reference_id",
        })
        self.assertEqual(set(audit["$defs"]["pairScore"]["required"]), {
            "C", "C_exact", "D", "D_exact", "F", "F_exact", "Q", "Q_exact",
            "T", "T_exact", "differentiated_feature_ids",
            "evidence_id", "label", "matched_feature_ids", "r_hi", "r_hi_exact",
            "r_obs", "r_obs_exact", "version",
        })

    @unittest.skipIf(Draft202012Validator is None, "optional jsonschema package is unavailable")
    def test_draft_2020_12_schemas_accept_runtime_exports_and_reject_malicious_nested_values(self):
        root = Path(__file__).resolve().parents[2]
        audit_schema = strict_json_loads((root / "schemas/audit.schema.json").read_text(encoding="utf-8"))
        feature_schema = strict_json_loads((root / "schemas/feature-map.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(audit_schema)
        Draft202012Validator.check_schema(feature_schema)
        audit_validator = Draft202012Validator(audit_schema)
        feature_validator = Draft202012Validator(feature_schema)

        hash_a, hash_b = "a" * 64, "b" * 64
        evidence_id = "ev_" + "1" * 16
        mapping = canonical_feature_map(feature_map(evidence_id))
        for item in mapping["features"].values():
            item["candidate_span_hashes"] = [hash_a]
        for item in mapping["reference_maps"][0]["decisions"].values():
            item["reference_span_hashes"] = [hash_b]
        valid_feature_export = {
            "corpus_set_hash": hash_a,
            "finalist_set_hash": hash_b,
            "maps": [
                {
                    "feature_map": deepcopy(mapping),
                    "finalist_id": "fi_" + str(index) * 20,
                    "map_id": "fm_" + str(index) * 20,
                }
                for index in range(1, 4)
            ],
            "run_id": "run-schema-fixture",
            "version": "feature-map-set-v1",
        }
        feature_validator.validate(valid_feature_export)

        score = score_pair(
            {"title": "same title", "abstract": "same abstract"},
            {
                "title": "same title", "abstract": "same abstract",
                "classifications": ["G06F 1/00"], "evidence_id": evidence_id,
            },
            mapping,
            self.config,
        )
        result = {
            "candidate_id": "ca_" + "1" * 20,
            "closest_reference_id": evidence_id,
            "corpus_hash": hash_a,
            "counterargument": "retrieved-corpus limitation",
            "coverage": score["Q"],
            "finalist_id": "fi_" + "1" * 20,
            "outcome": "decision_required",
            "pair_scores": [score],
            "r_hi": score["r_hi"],
            "r_obs": score["r_obs"],
            "upper_bound_reference_id": evidence_id,
        }
        valid_audit_export = {
            "corpus_set_hash": hash_a,
            "feature_map_set_hash": hash_b,
            "finalist_set_hash": hash_a,
            "results": [
                {**deepcopy(result), "candidate_id": "ca_" + str(index) * 20,
                 "finalist_id": "fi_" + str(index) * 20}
                for index in range(1, 4)
            ],
            "run_id": "run-schema-fixture",
            "scorer_config_hash": hash_b,
            "version": "audit-batch-v1",
        }
        audit_validator.validate(valid_audit_export)
        validate_audit_artifact(valid_audit_export, self.config)

        audit_attacks = {
            "C object": ("C", {}),
            "D negative number": ("D", -7),
            "F null": ("F", None),
            "Q array": ("Q", []),
            "T boolean": ("T", False),
            "empty evidence identity": ("evidence_id", ""),
            "bogus label": ("label", "unknown"),
            "object observed risk": ("r_obs", {}),
            "array upper risk": ("r_hi", []),
            "wrong scorer version": ("version", "simrisk-v9"),
            "string exact risk": ("r_obs_exact", "101"),
            "negative exact risk": ("r_obs_exact", {"numerator": -1, "denominator": 1, "value": -1}),
            "over-100 exact risk": ("Q_exact", {"numerator": 101, "denominator": 1, "value": 101}),
        }
        for name, (field, value) in audit_attacks.items():
            with self.subTest(schema="audit", attack=name):
                malicious = deepcopy(valid_audit_export)
                malicious["results"][0]["pair_scores"][0][field] = value
                with self.assertRaises(ValidationError):
                    audit_validator.validate(malicious)
        for field in ("T_exact", "F_exact", "C_exact", "D_exact", "Q_exact", "r_obs_exact", "r_hi_exact"):
            with self.subTest(schema="audit", attack=f"over-100 {field}"):
                malicious = deepcopy(valid_audit_export)
                malicious["results"][0]["pair_scores"][0][field] = {
                    "numerator": 101, "denominator": 1, "value": 101,
                }
                with self.assertRaises(ValidationError):
                    audit_validator.validate(malicious)
            for inconsistent in (
                {"numerator": 101, "denominator": 1, "value": 100},
                {"numerator": 1, "denominator": 3, "value": 0.34},
            ):
                with self.subTest(validator="artifact", field=field, inconsistent=inconsistent):
                    malicious = deepcopy(valid_audit_export)
                    malicious["results"][0]["pair_scores"][0][field] = inconsistent
                    with self.assertRaisesRegex(ValueError, "inconsistent|display"):
                        validate_audit_artifact(malicious, self.config)

        feature_attacks = {
            "bad candidate span": lambda value: value["maps"][0]["feature_map"]["features"]["feature-problem"].update(
                candidate_span_hashes=["not-a-hash"]
            ),
            "unknown inspected field": lambda value: value["maps"][0]["feature_map"]["reference_maps"][0].update(
                inspected_fields=["claims"]
            ),
            "matched without evidence span": lambda value: value["maps"][0]["feature_map"]["reference_maps"][0]["decisions"]["feature-problem"].update(
                status="matched", reference_span_hashes=[]
            ),
            "different without evidence span": lambda value: value["maps"][0]["feature_map"]["reference_maps"][0]["decisions"]["feature-problem"].update(
                status="different", reference_span_hashes=[]
            ),
            "non-disclosure without evidence span": lambda value: value["maps"][0]["feature_map"]["reference_maps"][0]["decisions"]["feature-problem"].update(
                status="not_disclosed", reference_span_hashes=[]
            ),
            "invalid reference span": lambda value: value["maps"][0]["feature_map"]["reference_maps"][0]["decisions"]["feature-problem"].update(
                reference_span_hashes=["not-a-hash"]
            ),
            "unknown decision status": lambda value: value["maps"][0]["feature_map"]["reference_maps"][0]["decisions"]["feature-problem"].update(
                status="unknown"
            ),
            "empty decision rationale": lambda value: value["maps"][0]["feature_map"]["reference_maps"][0]["decisions"]["feature-problem"].update(
                rationale="   "
            ),
            "unreviewed map": lambda value: value["maps"][0]["feature_map"]["review"].update(status="pending"),
        }
        for name, mutate in feature_attacks.items():
            with self.subTest(schema="feature-map", attack=name):
                malicious = deepcopy(valid_feature_export)
                mutate(malicious)
                with self.assertRaises(ValidationError):
                    feature_validator.validate(malicious)

        unavailable = deepcopy(valid_feature_export)
        unavailable_decision = unavailable["maps"][0]["feature_map"]["reference_maps"][0]["decisions"]["feature-problem"]
        unavailable_decision.update(status="unavailable", reference_span_hashes=[])
        feature_validator.validate(unavailable)


if __name__ == "__main__":
    unittest.main()
