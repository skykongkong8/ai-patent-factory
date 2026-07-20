"""Deterministic, advisory-only quality lint for finalists and audit corpora.

Pure functions over already-validated structures. Advisories never block a
gate, change state, or enter a hash-bound artifact — they ride the CLI result
so the driving agent can surface homogeneity and thin-coverage smells to the
user (silently replacing or blocking a finalist stays a human decision).
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

LINT_VERSION = "quality-lint-v1"
SCORE_EPSILON = 2
MIN_CORPUS_RECORDS = 3


def _advisory(code: str, subjects: Iterable[str], detail: str) -> dict[str, Any]:
    return {"code": code, "detail": detail, "subjects": sorted(subjects), "version": LINT_VERSION}


def shortlist_advisories(finalists: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flag internally flat axis vectors and near-identical finalist vectors."""

    advisories: list[dict[str, Any]] = []
    vectors: list[tuple[str, tuple[int, ...]]] = []
    for item in finalists:
        if not isinstance(item, Mapping):
            continue
        identity = str(item.get("finalist_id") or item.get("candidate_id") or "?")
        axes = [axis for axis in item.get("axes", []) if isinstance(axis, Mapping)]
        scores = tuple(
            int(axis.get("score", 0))
            for axis in sorted(axes, key=lambda axis: str(axis.get("axis", "")))
        )
        if not scores:
            continue
        vectors.append((identity, scores))
        if max(scores) - min(scores) == 0:
            advisories.append(_advisory(
                "flat_axis_scores", [identity],
                "every axis carries the same score — axis judgments may not be independent",
            ))
    for index, (first_id, first_scores) in enumerate(vectors):
        for second_id, second_scores in vectors[index + 1:]:
            if len(first_scores) == len(second_scores) and all(
                abs(a - b) <= SCORE_EPSILON for a, b in zip(first_scores, second_scores)
            ):
                advisories.append(_advisory(
                    "near_identical_finalists", [first_id, second_id],
                    f"axis-score vectors differ by at most {SCORE_EPSILON} points — "
                    "the finalists may not be genuinely distinct proposals",
                ))
    return sorted(advisories, key=lambda item: (item["code"], item["subjects"]))


def audit_advisories(
    corpus_set: Mapping[str, Any], audit: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Flag thin per-finalist corpora and a shared closest prior-art reference."""

    advisories: list[dict[str, Any]] = []
    for corpus in corpus_set.get("corpora", []):
        if not isinstance(corpus, Mapping):
            continue
        retained = corpus.get("retained_count")
        if isinstance(retained, int) and retained < MIN_CORPUS_RECORDS:
            advisories.append(_advisory(
                "thin_corpus", [str(corpus.get("finalist_id", "?"))],
                f"only {retained} retained record(s) (< {MIN_CORPUS_RECORDS}) — similarity "
                "figures are bounded by a very small corpus; treat the risk numbers as weak evidence",
            ))
    closest: dict[str, list[str]] = {}
    for result in audit.get("results", []):
        if not isinstance(result, Mapping):
            continue
        reference = result.get("closest_reference_id")
        if isinstance(reference, str) and reference:
            closest.setdefault(reference, []).append(str(result.get("finalist_id", "?")))
    for reference, finalist_ids in sorted(closest.items()):
        if len(finalist_ids) > 1:
            advisories.append(_advisory(
                "shared_closest_reference", finalist_ids,
                f"multiple finalists share the same closest prior-art reference ({reference}) — "
                "they may overlap more than the axis scores suggest",
            ))
    return sorted(advisories, key=lambda item: (item["code"], item["subjects"]))


__all__ = [
    "LINT_VERSION", "MIN_CORPUS_RECORDS", "SCORE_EPSILON",
    "audit_advisories", "shortlist_advisories",
]
