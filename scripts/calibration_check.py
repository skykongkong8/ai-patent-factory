#!/usr/bin/env python3
"""Validate independent SimRisk calibration evidence without inventing labels."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from patent_factory.config import load_similarity_config
from patent_factory.provenance import canonical_json, digest, strict_json_loads


CALIBRATION_VERSION = "calibration-manifest-v1"
CALIBRATION_TRUST_VERSION = "calibration-trust-v1.0.0"
CALIBRATION_TRUST_PATH = ROOT / "config" / "calibration-trust-v1.0.0.json"
QUALIFIED_ROLES = frozenset({"domain_expert", "patent_attorney"})
RISK_LABELS = frozenset({"low", "moderate", "high", "excessive"})
SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CalibrationResult:
    calibration_status: str
    release_status: str
    code: str
    manifest_hash: str | None
    record_count: int
    reviewer_count: int
    scorer_version: str
    scorer_config_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "calibration_status": self.calibration_status,
            "code": self.code,
            "manifest_hash": self.manifest_hash,
            "record_count": self.record_count,
            "release_status": self.release_status,
            "reviewer_count": self.reviewer_count,
            "schema_version": "calibration-check-result-v1",
            "scorer_config_hash": self.scorer_config_hash,
            "scorer_version": self.scorer_version,
        }


def _deferred(code: str) -> CalibrationResult:
    config = load_similarity_config()
    return CalibrationResult(
        "deferred_provisional", "review_blocked", code, None, 0, 0,
        config.version, config.content_hash,
    )


def _required_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: non-empty string required")
    return value.strip()


def _sha256(value: Any, path: str) -> str:
    text = _required_text(value, path)
    if SHA256.fullmatch(text) is None:
        raise ValueError(f"{path}: sha256 required")
    return text


def _canonical_utc(value: Any, path: str) -> str:
    text = _required_text(value, path)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"{path}: canonical UTC timestamp required") from exc
    if parsed.isoformat(timespec="seconds").replace("+00:00", "Z") != text:
        raise ValueError(f"{path}: canonical UTC timestamp required")
    return text


def validate_calibration_trust(value: Mapping[str, Any]) -> dict[str, str | None]:
    required = {"approved_manifest_hash", "schema_version"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("calibration_trust: exact fields required")
    if value.get("schema_version") != CALIBRATION_TRUST_VERSION:
        raise ValueError("calibration_trust.schema_version: unsupported")
    approved = value.get("approved_manifest_hash")
    if approved is not None:
        approved = _sha256(approved, "calibration_trust.approved_manifest_hash")
    return {
        "approved_manifest_hash": approved,
        "schema_version": CALIBRATION_TRUST_VERSION,
    }


def load_calibration_trust() -> dict[str, str | None]:
    value = strict_json_loads(CALIBRATION_TRUST_PATH.read_bytes())
    if not isinstance(value, Mapping):
        raise ValueError("calibration_trust: object required")
    return validate_calibration_trust(value)


def apply_calibration_trust(
    result: CalibrationResult, trust: Mapping[str, Any],
) -> CalibrationResult:
    validated = validate_calibration_trust(trust)
    if (
        result.calibration_status != "qualified_independent"
        or result.manifest_hash is None
        or validated["approved_manifest_hash"] != result.manifest_hash
    ):
        return result
    return CalibrationResult(
        result.calibration_status, "eligible", "calibration_trusted",
        result.manifest_hash, result.record_count, result.reviewer_count,
        result.scorer_version, result.scorer_config_hash,
    )


def validate_calibration_manifest(value: Mapping[str, Any]) -> CalibrationResult:
    required = {
        "calibration_kind", "producer_id", "records", "reviewers", "schema_version",
        "scorer_config_hash", "scorer_version",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("calibration_manifest: exact fields required")
    if value.get("schema_version") != CALIBRATION_VERSION:
        raise ValueError("calibration_manifest.schema_version: unsupported")
    if value.get("calibration_kind") != "independent_redacted":
        raise ValueError("calibration_manifest.calibration_kind: synthetic data is not calibration")

    config = load_similarity_config()
    if value.get("scorer_version") != config.version:
        raise ValueError("calibration_manifest.scorer_version: current scorer version required")
    if value.get("scorer_config_hash") != config.content_hash:
        raise ValueError("calibration_manifest.scorer_config_hash: current scorer config required")

    producer_id = _required_text(value.get("producer_id"), "calibration_manifest.producer_id")
    raw_reviewers = value.get("reviewers")
    if not isinstance(raw_reviewers, list) or len(raw_reviewers) < 2:
        raise ValueError("calibration_manifest.reviewers: at least two independent reviewers required")
    reviewers: dict[str, str] = {}
    for index, item in enumerate(raw_reviewers):
        path = f"calibration_manifest.reviewers[{index}]"
        if not isinstance(item, Mapping) or set(item) != {
            "affiliation", "independent", "reviewed_at", "reviewer_id", "role"
        }:
            raise ValueError(f"{path}: exact reviewer fields required")
        reviewer_id = _required_text(item.get("reviewer_id"), f"{path}.reviewer_id")
        role = _required_text(item.get("role"), f"{path}.role")
        _required_text(item.get("affiliation"), f"{path}.affiliation")
        _canonical_utc(item.get("reviewed_at"), f"{path}.reviewed_at")
        if item.get("independent") is not True or reviewer_id == producer_id:
            raise ValueError(f"{path}: self-review or non-independent review rejected")
        if role not in QUALIFIED_ROLES:
            raise ValueError(f"{path}.role: qualified domain or patent reviewer required")
        if reviewer_id in reviewers:
            raise ValueError(f"{path}.reviewer_id: duplicate reviewer")
        reviewers[reviewer_id] = role

    raw_records = value.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("calibration_manifest.records: non-empty records required")
    case_ids: set[str] = set()
    for index, item in enumerate(raw_records):
        path = f"calibration_manifest.records[{index}]"
        if not isinstance(item, Mapping) or set(item) != {
            "case_id", "corpus_hash", "disagreement", "input_hash", "labels",
            "threshold_sensitivity_hash",
        }:
            raise ValueError(f"{path}: exact record fields required")
        case_id = _required_text(item.get("case_id"), f"{path}.case_id")
        if case_id in case_ids:
            raise ValueError(f"{path}.case_id: duplicate case")
        case_ids.add(case_id)
        _sha256(item.get("corpus_hash"), f"{path}.corpus_hash")
        _sha256(item.get("input_hash"), f"{path}.input_hash")
        _sha256(item.get("threshold_sensitivity_hash"), f"{path}.threshold_sensitivity_hash")
        labels = item.get("labels")
        if not isinstance(labels, list) or len(labels) != len(reviewers):
            raise ValueError(f"{path}.labels: one label per independent reviewer required")
        resolved: dict[str, str] = {}
        for label_index, label in enumerate(labels):
            label_path = f"{path}.labels[{label_index}]"
            if not isinstance(label, Mapping) or set(label) != {
                "rationale_hash", "reviewer_id", "risk_label"
            }:
                raise ValueError(f"{label_path}: exact label fields required")
            reviewer_id = _required_text(label.get("reviewer_id"), f"{label_path}.reviewer_id")
            if reviewer_id not in reviewers or reviewer_id in resolved:
                raise ValueError(f"{label_path}.reviewer_id: unknown or duplicate reviewer")
            risk_label = _required_text(label.get("risk_label"), f"{label_path}.risk_label")
            if risk_label not in RISK_LABELS:
                raise ValueError(f"{label_path}.risk_label: unsupported risk label")
            _sha256(label.get("rationale_hash"), f"{label_path}.rationale_hash")
            resolved[reviewer_id] = risk_label
        if set(resolved) != set(reviewers):
            raise ValueError(f"{path}.labels: incomplete reviewer coverage")
        disagreement = item.get("disagreement")
        if not isinstance(disagreement, Mapping) or set(disagreement) != {"present", "resolution_hash"}:
            raise ValueError(f"{path}.disagreement: exact fields required")
        expected_disagreement = len(set(resolved.values())) > 1
        if disagreement.get("present") is not expected_disagreement:
            raise ValueError(f"{path}.disagreement.present: does not match labels")
        resolution_hash = disagreement.get("resolution_hash")
        if expected_disagreement:
            _sha256(resolution_hash, f"{path}.disagreement.resolution_hash")
        elif resolution_hash is not None:
            raise ValueError(f"{path}.disagreement.resolution_hash: must be null without disagreement")

    return CalibrationResult(
        "qualified_independent", "review_blocked", "calibration_untrusted", digest(dict(value)),
        len(raw_records), len(reviewers), config.version, config.content_hash,
    )


def check_calibration(path: Path | None) -> CalibrationResult:
    if path is None or not path.is_file():
        return _deferred("independent_calibration_absent")
    try:
        value = strict_json_loads(path.read_bytes())
        if not isinstance(value, Mapping):
            raise ValueError("calibration_manifest: object required")
        structural = validate_calibration_manifest(value)
        return apply_calibration_trust(structural, load_calibration_trust())
    except (OSError, UnicodeError, ValueError) as exc:
        result = _deferred("independent_calibration_invalid")
        # Diagnostics are deliberately structural and never echo labels or source text.
        return CalibrationResult(
            result.calibration_status, result.release_status,
            f"{result.code}:{type(exc).__name__}", None, 0, 0,
            result.scorer_version, result.scorer_config_hash,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path,
        default=Path("tests/fixtures/similarity/calibration/manifest.json"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    result = check_calibration(build_parser().parse_args(argv).manifest)
    print(canonical_json(result.as_dict()))
    return 0 if result.release_status == "eligible" else 3


if __name__ == "__main__":
    raise SystemExit(main())
