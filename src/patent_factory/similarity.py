from __future__ import annotations

import re
import unicodedata
from fractions import Fraction
from typing import Any, Mapping, Sequence

from .config import SimilarityConfig
from .provenance import normalize


FEATURE_STATUSES = frozenset({"matched", "different", "not_disclosed", "unavailable"})
EXACT_SCORE_FIELDS = ("T", "F", "C", "D", "Q", "r_obs", "r_hi")


def _fraction(value: str | int | Fraction) -> Fraction:
    result = value if isinstance(value, Fraction) else Fraction(value)
    if result < 0:
        raise ValueError("simrisk: negative value")
    return result


def _display(value: Fraction) -> str:
    scaled = value * 100
    if scaled.denominator == 1:
        return str(scaled.numerator)
    return f"{float(scaled):.6f}".rstrip("0").rstrip(".")


def _display_percent(value: Fraction) -> str:
    return _display(value / 100)


def _quantized_percent_value(value: Fraction) -> int | float:
    display = _display_percent(value)
    return int(display) if "." not in display else float(display)


def _exact(value: Fraction) -> dict[str, int | float]:
    if not 0 <= value <= 100:
        raise ValueError("simrisk: exact percentage must be within [0,100]")
    return {
        "denominator": value.denominator,
        "numerator": value.numerator,
        "value": _quantized_percent_value(value),
    }


def _score_fraction(score: Mapping[str, Any], name: str) -> Fraction:
    exact = score.get(f"{name}_exact")
    if exact is None:
        result = Fraction(score[name])
    else:
        if not isinstance(exact, Mapping) or set(exact) != {"denominator", "numerator", "value"}:
            raise ValueError(f"simrisk.{name}_exact: closed exact percentage required")
        numerator, denominator, value = exact["numerator"], exact["denominator"], exact["value"]
        if (
            isinstance(numerator, bool) or not isinstance(numerator, int) or numerator < 0
            or isinstance(denominator, bool) or not isinstance(denominator, int) or denominator < 1
            or isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100
        ):
            raise ValueError(f"simrisk.{name}_exact: bounded rational percentage required")
        result = Fraction(numerator, denominator)
        if not 0 <= result <= 100 or value != _quantized_percent_value(result):
            raise ValueError(f"simrisk.{name}_exact: inconsistent or out-of-range percentage")
    if not 0 <= result <= 100:
        raise ValueError(f"simrisk.{name}: percentage must be within [0,100]")
    return result


def validate_pair_score(score: Mapping[str, Any]) -> dict[str, Fraction]:
    """Validate every exact rational and its canonical six-decimal display mirror."""

    if not isinstance(score, Mapping):
        raise ValueError("simrisk.pair_score: object required")
    fractions: dict[str, Fraction] = {}
    for name in EXACT_SCORE_FIELDS:
        if name not in score or f"{name}_exact" not in score:
            raise ValueError(f"simrisk.pair_score: {name} display and exact values required")
        fraction = _score_fraction(score, name)
        if score[name] != _display_percent(fraction):
            raise ValueError(f"simrisk.{name}: display does not match exact percentage")
        fractions[name] = fraction
    if score.get("version") != "simrisk-v1.0.0":
        raise ValueError("simrisk.pair_score: scorer version mismatch")
    return fractions


def risk_label(value: Fraction, config: SimilarityConfig) -> str:
    excessive, high, moderate = (Fraction(config.thresholds[name]) for name in ("excessive", "high", "moderate"))
    return "excessive" if value >= excessive else "high" if value >= high else "moderate" if value >= moderate else "low"


def normalized_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", normalize(value)).casefold()
    return " ".join(re.findall(r"[0-9a-z가-힣]+", text))


def token_jaccard(left: str | None, right: str | None) -> Fraction:
    a, b = set(normalized_text(left).split()), set(normalized_text(right).split())
    if not a or not b:
        return Fraction(0)
    return Fraction(len(a & b), len(a | b))


def _trigrams(value: str | None) -> set[str]:
    text = normalized_text(value).replace(" ", "")
    if not text:
        return set()
    if len(text) < 3:
        return {text}
    return {text[index:index + 3] for index in range(len(text) - 2)}


def trigram_dice(left: str | None, right: str | None) -> Fraction:
    a, b = _trigrams(left), _trigrams(right)
    if not a or not b:
        return Fraction(0)
    return Fraction(2 * len(a & b), len(a) + len(b))


def text_overlap(left: str | None, right: str | None) -> Fraction:
    return (token_jaccard(left, right) + trigram_dice(left, right)) / 2


def _classification_similarity(candidate: Sequence[str], reference: Sequence[str], config: SimilarityConfig) -> tuple[Fraction, bool]:
    left = [re.sub(r"[^A-Z0-9/]", "", value.upper()) for value in candidate if normalize(value)]
    right = [re.sub(r"[^A-Z0-9/]", "", value.upper()) for value in reference if normalize(value)]
    if not left or not right:
        return Fraction(0), False
    scores = {name: Fraction(value) for name, value in config.classification_scores.items()}
    best = Fraction(0)
    for a in left:
        for b in right:
            if a == b:
                score = scores["subgroup"]
            elif a.split("/", 1)[0] == b.split("/", 1)[0]:
                score = scores["main_group"]
            elif a[:4] == b[:4]:
                score = scores["subclass"]
            elif a[:1] == b[:1]:
                score = scores["section"]
            else:
                score = scores["unrelated"]
            best = max(best, score)
    return best, True


def canonical_feature_map(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize legacy list input only after rejecting duplicate feature identities."""

    required = {"candidate_classifications", "features", "reference_maps", "review"}
    if not isinstance(value, Mapping) or set(value) != required or not isinstance(value["reference_maps"], list):
        raise ValueError("feature_map: exact fields required")
    raw_features = value["features"]
    features: dict[str, Any] = {}
    if isinstance(raw_features, list):
        for feature in raw_features:
            if not isinstance(feature, Mapping) or set(feature) != {
                "candidate_span_hashes", "category", "essential", "feature_id", "weight",
            }:
                raise ValueError("feature_map.features: exact fields required")
            feature_id = normalize(feature["feature_id"])
            if not isinstance(feature_id, str) or not feature_id or feature_id in features:
                raise ValueError("feature_map.features: duplicate or empty feature identity")
            features[feature_id] = {name: feature[name] for name in (
                "candidate_span_hashes", "category", "essential", "weight",
            )}
    elif isinstance(raw_features, Mapping):
        for raw_id, feature in raw_features.items():
            feature_id = normalize(raw_id)
            if not isinstance(feature_id, str) or not feature_id or feature_id in features or not isinstance(feature, Mapping):
                raise ValueError("feature_map.features: duplicate or empty feature identity")
            if set(feature) != {"candidate_span_hashes", "category", "essential", "weight"}:
                raise ValueError("feature_map.features: exact fields required")
            features[feature_id] = dict(feature)
    else:
        raise ValueError("feature_map.features: keyed feature object required")

    reference_maps = []
    for mapping in value["reference_maps"]:
        if not isinstance(mapping, Mapping) or set(mapping) != {"decisions", "evidence_id", "inspected_fields"}:
            raise ValueError("feature_map.reference_maps: exact fields required")
        raw_decisions = mapping["decisions"]
        decisions: dict[str, Any] = {}
        if isinstance(raw_decisions, list):
            for decision in raw_decisions:
                if not isinstance(decision, Mapping) or set(decision) != {
                    "feature_id", "rationale", "reference_span_hashes", "status",
                }:
                    raise ValueError("feature_map.decision: invalid fields or status")
                feature_id = normalize(decision["feature_id"])
                if not isinstance(feature_id, str) or not feature_id or feature_id in decisions:
                    raise ValueError("feature_map.reference_maps: duplicate or empty decision identity")
                decisions[feature_id] = {name: decision[name] for name in (
                    "rationale", "reference_span_hashes", "status",
                )}
        elif isinstance(raw_decisions, Mapping):
            for raw_id, decision in raw_decisions.items():
                feature_id = normalize(raw_id)
                if not isinstance(feature_id, str) or not feature_id or feature_id in decisions or not isinstance(decision, Mapping):
                    raise ValueError("feature_map.reference_maps: duplicate or empty decision identity")
                if set(decision) != {"rationale", "reference_span_hashes", "status"}:
                    raise ValueError("feature_map.decision: invalid fields or status")
                decisions[feature_id] = dict(decision)
        else:
            raise ValueError("feature_map.reference_maps: keyed decisions object required")
        reference_maps.append({
            "decisions": decisions,
            "evidence_id": mapping["evidence_id"],
            "inspected_fields": mapping["inspected_fields"],
        })
    return normalize({
        "candidate_classifications": value["candidate_classifications"],
        "features": features,
        "reference_maps": reference_maps,
        "review": value["review"],
    })


def validate_feature_map(value: Mapping[str, Any], config: SimilarityConfig) -> None:
    required = {"candidate_classifications", "features", "reference_maps", "review"}
    if set(value) != required or not isinstance(value["features"], Mapping) or not isinstance(value["reference_maps"], list):
        raise ValueError("feature_map: exact fields required")
    review = value["review"]
    if not isinstance(review, Mapping) or set(review) != {"reviewed_at", "reviewed_by", "status"} or review["status"] != "reviewed":
        raise ValueError("feature_map.review: frozen reviewed attestation required")
    if any(
        not isinstance(normalize(review[name]), str) or not normalize(review[name])
        for name in ("reviewed_at", "reviewed_by")
    ):
        raise ValueError("feature_map.review: reviewer identity and timestamp required")
    totals = {name: Fraction(0) for name in config.feature_weights}
    for feature_id, feature in value["features"].items():
        if not normalize(feature_id) or not isinstance(feature, Mapping) or set(feature) != {
            "candidate_span_hashes", "category", "essential", "weight",
        }:
            raise ValueError("feature_map.features: exact fields required")
        category = feature["category"]
        if category not in totals or not isinstance(feature["essential"], bool):
            raise ValueError("feature_map.features: invalid identity or category")
        if not isinstance(feature["candidate_span_hashes"], list) or not feature["candidate_span_hashes"]:
            raise ValueError("feature_map.features: candidate spans required")
        totals[category] += _fraction(feature["weight"])
    expected = {name: Fraction(weight) for name, weight in config.feature_weights.items()}
    if totals != expected:
        raise ValueError("feature_map.features: weights must equal simrisk-v1.0.0 category weights")
    evidence_ids: set[str] = set()
    for mapping in value["reference_maps"]:
        fields = {"decisions", "evidence_id", "inspected_fields"}
        if not isinstance(mapping, Mapping) or set(mapping) != fields or mapping["evidence_id"] in evidence_ids:
            raise ValueError("feature_map.reference_maps: exact distinct evidence maps required")
        evidence_ids.add(mapping["evidence_id"])
        if not isinstance(mapping["inspected_fields"], list) or not mapping["inspected_fields"]:
            raise ValueError("feature_map.reference_maps: inspected fields required")
        decisions = mapping["decisions"]
        if not isinstance(decisions, Mapping) or set(decisions) != set(value["features"]):
            raise ValueError("feature_map.reference_maps: one decision per feature required")
        for feature_id, decision in decisions.items():
            if (
                not normalize(feature_id) or not isinstance(decision, Mapping)
                or set(decision) != {"rationale", "reference_span_hashes", "status"}
                or decision["status"] not in FEATURE_STATUSES
            ):
                raise ValueError("feature_map.decision: invalid fields or status")
            if not isinstance(normalize(decision["rationale"]), str) or not normalize(decision["rationale"]):
                raise ValueError("feature_map.decision: nonempty rationale required")
            spans = decision["reference_span_hashes"]
            if not isinstance(spans, list):
                raise ValueError("feature_map.decision: reference spans array required")
            if decision["status"] in {"matched", "different", "not_disclosed"} and not spans:
                raise ValueError("feature_map.decision: positive evidence requires a source span")
            if decision["status"] == "not_disclosed" and not mapping["inspected_fields"]:
                raise ValueError("feature_map.decision: non-disclosure requires inspected fields")


def score_pair(candidate: Mapping[str, Any], reference: Mapping[str, Any], feature_map: Mapping[str, Any], config: SimilarityConfig) -> dict[str, Any]:
    feature_map = canonical_feature_map(feature_map)
    validate_feature_map(feature_map, config)
    evidence_id = reference["evidence_id"]
    mapping = next((item for item in feature_map["reference_maps"] if item["evidence_id"] == evidence_id), None)
    if mapping is None:
        raise ValueError("feature_map: retained reference is not reviewed")
    decisions = mapping["decisions"]

    title_available = bool(normalized_text(candidate.get("title")) and normalized_text(reference.get("title")))
    abstract_available = bool(normalized_text(candidate.get("abstract")) and normalized_text(reference.get("abstract")))
    title = text_overlap(candidate.get("title"), reference.get("title"))
    abstract = text_overlap(candidate.get("abstract"), reference.get("abstract"))
    tw = {name: Fraction(value) for name, value in config.text_weights.items()}
    t_obs = tw["title"] * title + tw["abstract"] * abstract
    t_hi = tw["title"] * (title if title_available else 1) + tw["abstract"] * (abstract if abstract_available else 1)
    q_t = tw["title"] * int(title_available) + tw["abstract"] * int(abstract_available)

    matched = Fraction(0)
    available = Fraction(0)
    essential_total = Fraction(0)
    differentiated = Fraction(0)
    matched_ids: list[str] = []
    differentiated_ids: list[str] = []
    for feature_id, feature in feature_map["features"].items():
        weight = Fraction(feature["weight"])
        decision = decisions[feature_id]
        if decision["status"] != "unavailable":
            available += weight
        if decision["status"] == "matched":
            matched += weight
            matched_ids.append(feature_id)
        if feature["essential"]:
            essential_total += weight
            if decision["status"] in {"different", "not_disclosed"}:
                differentiated += weight
                differentiated_ids.append(feature_id)
    f_obs, f_hi, q_f = matched, matched + (1 - available), available
    d = differentiated / essential_total if essential_total else Fraction(0)
    c_obs, c_available = _classification_similarity(feature_map["candidate_classifications"], reference.get("classifications", ()), config)
    c_hi, q_c = (c_obs if c_available else Fraction(1)), Fraction(int(c_available))

    aw = {name: Fraction(value) for name, value in config.aggregate_weights.items()}
    clamp = lambda value: min(Fraction(1), max(Fraction(0), value))
    r_obs = 100 * clamp(aw["text"] * t_obs + aw["features"] * f_obs + aw["classification"] * c_obs - aw["difference_credit"] * d)
    r_hi = 100 * clamp(aw["text"] * t_hi + aw["features"] * f_hi + aw["classification"] * c_hi - aw["difference_credit"] * d)
    q = aw["text"] * q_t + aw["features"] * q_f + aw["classification"] * q_c
    label = risk_label(r_obs, config)
    return {
        "C": _display(c_obs), "C_exact": _exact(100 * c_obs),
        "D": _display(d), "D_exact": _exact(100 * d),
        "F": _display(f_obs), "F_exact": _exact(100 * f_obs),
        "Q": _display(q), "Q_exact": _exact(100 * q),
        "T": _display(t_obs), "T_exact": _exact(100 * t_obs),
        "evidence_id": evidence_id, "label": label, "matched_feature_ids": sorted(matched_ids),
        "differentiated_feature_ids": sorted(differentiated_ids), "r_hi": _display(r_hi / 100),
        "r_obs": _display(r_obs / 100),
        "r_hi_exact": _exact(r_hi), "r_obs_exact": _exact(r_obs), "version": config.version,
    }


def summarize_candidate(scores: Sequence[Mapping[str, Any]], config: SimilarityConfig) -> dict[str, Any]:
    if not scores:
        return {
            "coverage": "0", "observed_reference_id": None, "outcome": "coverage_insufficient",
            "r_hi": "100", "r_obs": "0", "upper_bound_reference_id": None,
        }
    validated = [(score, validate_pair_score(score)) for score in scores]
    excessive = Fraction(config.thresholds["excessive"])
    coverage = 100 * Fraction(config.thresholds["coverage"])
    observed, observed_exact = max(
        validated, key=lambda item: (item[1]["r_obs"], item[1]["r_hi"], item[0]["evidence_id"])
    )
    maximum_upper = max(exact["r_hi"] for _score, exact in validated)
    upper, upper_exact = min(
        (item for item in validated if item[1]["r_hi"] == maximum_upper),
        key=lambda item: (item[1]["Q"], item[0]["evidence_id"]),
    )
    if any(exact["r_obs"] >= excessive for _score, exact in validated):
        outcome = "decision_required"
    elif upper_exact["Q"] < coverage or maximum_upper >= excessive:
        outcome = "coverage_insufficient"
    else:
        outcome = "audit_approved"
    return {
        "coverage": upper["Q"], "observed_reference_id": observed["evidence_id"],
        "outcome": outcome, "r_hi": upper["r_hi"], "r_obs": observed["r_obs"],
        "upper_bound_reference_id": upper["evidence_id"],
    }


def candidate_outcome(scores: Sequence[Mapping[str, Any]], config: SimilarityConfig) -> str:
    return summarize_candidate(scores, config)["outcome"]
