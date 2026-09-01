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


def candidate_advisories(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flag homogeneous candidate inputs without blocking publication."""

    items = [item for item in candidates if isinstance(item, Mapping)]
    advisories: list[dict[str, Any]] = []
    if len(items) < 2:
        return advisories

    identities = [
        str(item.get("candidate_id") or f"candidate[{index}]")
        for index, item in enumerate(items)
    ]
    methods = {
        str(trace.get("method"))
        for item in items
        if isinstance((trace := item.get("synthesis_trace")), Mapping)
    }
    if len(methods) == 1:
        advisories.append(_advisory(
            "single_synthesis_method", identities,
            "every candidate uses the same synthesis_trace.method; consider adding another creative operation",
        ))

    evidence_sets = {
        tuple(sorted(
            str(reference.get("evidence_id"))
            for reference in item.get("evidence_references", [])
            if isinstance(reference, Mapping) and reference.get("evidence_id")
        ))
        for item in items
    }
    if len(evidence_sets) == 1:
        advisories.append(_advisory(
            "identical_evidence_sets", identities,
            "every candidate cites the same evidence ID set; evidence coverage may be too narrow",
        ))

    profile_sets = {
        tuple(sorted(
            f"{reference.get('field')}:{reference.get('claim_id')}:{reference.get('kind')}"
            for reference in item.get("profile_references", [])
            if isinstance(reference, Mapping) and reference.get("field") and reference.get("claim_id")
        ))
        for item in items
    }
    if len(profile_sets) == 1:
        advisories.append(_advisory(
            "identical_profile_reference_sets", identities,
            "every candidate cites the same profile fact set; consider varying the user problem/capability angle",
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


def workbench_advisories(
    ideas: Iterable[Mapping[str, Any]],
    relations: Iterable[Mapping[str, Any]],
    candidate_input: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Flag divergence workbench smells without blocking validation."""

    advisories: list[dict[str, Any]] = []
    idea_list = [item for item in ideas if isinstance(item, Mapping)]
    sessions = {str(item.get("session_id")) for item in idea_list if item.get("session_id")}
    lenses = {str(item.get("lens")) for item in idea_list if item.get("lens")}
    if len(lenses) == 1 and len(idea_list) > 1:
        advisories.append(_advisory(
            "single_lens_workbench",
            sorted(str(item.get("idea_id", "?")) for item in idea_list),
            "all workbench ideas use one lens; divergence may be too narrow",
        ))
    relation_list = [item for item in relations if isinstance(item, Mapping)]
    cross_session = False
    idea_sessions = {str(item.get("idea_id")): str(item.get("session_id")) for item in idea_list}
    for relation in relation_list:
        ids = [str(item) for item in relation.get("source_idea_ids", [])]
        ids.extend(str(item) for item in relation.get("target_idea_ids", []))
        if len({idea_sessions.get(item) for item in ids if item in idea_sessions}) > 1:
            cross_session = True
            break
    if len(sessions) > 1 and relation_list and not cross_session:
        advisories.append(_advisory(
            "no_cross_session_relation",
            sorted(sessions),
            "multiple sessions exist but no relation connects ideas across sessions",
        ))
    if candidate_input and isinstance(candidate_input.get("candidates"), list):
        methods = {
            str(candidate.get("synthesis_trace", {}).get("method"))
            for candidate in candidate_input["candidates"]
            if isinstance(candidate, Mapping) and isinstance(candidate.get("synthesis_trace"), Mapping)
        }
        if len(methods) == 1 and len(candidate_input["candidates"]) > 1:
            advisories.append(_advisory(
                "single_synthesis_method",
                sorted(methods),
                "all promoted candidates use the same synthesis method",
            ))
    return sorted(advisories, key=lambda item: (item["code"], item["subjects"]))


__all__ = [
    "LINT_VERSION", "MIN_CORPUS_RECORDS", "SCORE_EPSILON",
    "audit_advisories", "candidate_advisories", "shortlist_advisories", "workbench_advisories",
]
