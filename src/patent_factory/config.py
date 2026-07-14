from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .provenance import digest, normalize, strict_json_loads


DEFAULT_CONFIG_PATH = Path("config/defaults.json")
DEFAULT_SIMILARITY_CONFIG_PATH = Path("config/simrisk-v1.0.0.json")


@dataclass(frozen=True)
class EvaluationConfig:
    schema_version: str
    candidate_schema_version: str
    finalist_schema_version: str
    minimum_finalists: int
    rubrics: Mapping[str, str]

    def validate(self) -> None:
        if self.schema_version != "factory-defaults-v1":
            raise ValueError("evaluation_config.schema_version: unsupported")
        if self.candidate_schema_version != "candidate-v1":
            raise ValueError("evaluation_config.candidate_schema_version: unsupported")
        if self.finalist_schema_version != "finalist-v1":
            raise ValueError("evaluation_config.finalist_schema_version: unsupported")
        if self.minimum_finalists != 3:
            raise ValueError("evaluation_config.minimum_finalists: v1 requires exactly 3")
        required = {"differentiation", "technical_feasibility", "utility_significance"}
        if set(self.rubrics) != required or any(not normalize(value) for value in self.rubrics.values()):
            raise ValueError("evaluation_config.rubrics: exact required axes and versions required")
        if str(self.rubrics["differentiation"]).startswith("simrisk-"):
            raise ValueError("evaluation_config.rubrics: G004 cannot use the final similarity scorer")

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return normalize({
            "candidate_schema_version": self.candidate_schema_version,
            "finalist_schema_version": self.finalist_schema_version,
            "minimum_finalists": self.minimum_finalists,
            "rubrics": dict(self.rubrics),
            "schema_version": self.schema_version,
        })

    @property
    def content_hash(self) -> str:
        return digest(self.as_dict())


def load_evaluation_config(path: Path = DEFAULT_CONFIG_PATH) -> EvaluationConfig:
    payload = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("evaluation_config: JSON object required")
    allowed = {
        "candidate_schema_version", "finalist_schema_version", "minimum_finalists",
        "rubrics", "schema_version",
    }
    if set(payload) != allowed or not isinstance(payload.get("rubrics"), dict):
        raise ValueError("evaluation_config: exact documented fields required")
    config = EvaluationConfig(
        schema_version=payload["schema_version"],
        candidate_schema_version=payload["candidate_schema_version"],
        finalist_schema_version=payload["finalist_schema_version"],
        minimum_finalists=payload["minimum_finalists"],
        rubrics=payload["rubrics"],
    )
    config.validate()
    return config


@dataclass(frozen=True)
class SimilarityConfig:
    version: str
    aggregate_weights: Mapping[str, str]
    text_weights: Mapping[str, str]
    feature_weights: Mapping[str, str]
    classification_scores: Mapping[str, str]
    thresholds: Mapping[str, str]
    tokenizer: Mapping[str, Any]
    corpus_limit: int
    page_cap: int
    results_per_query: int

    def validate(self) -> None:
        from fractions import Fraction

        if self.version != "simrisk-v1.0.0":
            raise ValueError("similarity_config.version: simrisk-v1.0.0 required")
        canonical = {
            "aggregate_weights": {"text": "0.25", "features": "0.60", "classification": "0.15", "difference_credit": "0.20"},
            "text_weights": {"title": "0.25", "abstract": "0.75"},
            "feature_weights": {"problem": "0.10", "inputs": "0.10", "mechanism": "0.30", "transformations": "0.20", "outputs": "0.10", "technical_effects": "0.20"},
            "classification_scores": {"subgroup": "1", "main_group": "0.80", "subclass": "0.55", "section": "0.25", "unrelated": "0"},
            "thresholds": {"low": "0", "moderate": "35", "high": "55", "excessive": "75", "coverage": "0.80"},
        }
        for name, expected in canonical.items():
            if dict(getattr(self, name)) != expected:
                raise ValueError(f"similarity_config.{name}: simrisk-v1.0.0 canonical values required; version bump required for drift")
            for value in expected.values():
                Fraction(value)
        expected_tokenizer = {
            "casefold": True, "char_ngram": 3, "normalization": "NFKC",
            "overlap_metrics": ["token_jaccard", "char_trigram_dice"],
            "token_pattern": "[0-9a-z가-힣]+",
        }
        if dict(self.tokenizer) != expected_tokenizer:
            raise ValueError("similarity_config.tokenizer: simrisk-v1.0.0 canonical parameters required")
        if (self.corpus_limit, self.page_cap, self.results_per_query) != (100, 5, 100):
            raise ValueError("similarity_config: v1 corpus budgets are fixed")

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return normalize({
            "aggregate_weights": dict(self.aggregate_weights),
            "classification_scores": dict(self.classification_scores),
            "corpus_limit": self.corpus_limit,
            "feature_weights": dict(self.feature_weights),
            "page_cap": self.page_cap,
            "results_per_query": self.results_per_query,
            "text_weights": dict(self.text_weights),
            "thresholds": dict(self.thresholds),
            "tokenizer": dict(self.tokenizer),
            "version": self.version,
        })

    @property
    def content_hash(self) -> str:
        return digest(self.as_dict())


def load_similarity_config(path: Path = DEFAULT_SIMILARITY_CONFIG_PATH) -> SimilarityConfig:
    payload = strict_json_loads(path.read_text(encoding="utf-8"))
    fields = {
        "aggregate_weights", "classification_scores", "corpus_limit", "feature_weights",
        "page_cap", "results_per_query", "text_weights", "thresholds", "tokenizer", "version",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("similarity_config: exact documented fields required")
    mappings = ("aggregate_weights", "classification_scores", "feature_weights", "text_weights", "thresholds", "tokenizer")
    if any(not isinstance(payload[name], dict) for name in mappings):
        raise ValueError("similarity_config: weight and threshold objects required")
    config = SimilarityConfig(**payload)
    config.validate()
    return config
