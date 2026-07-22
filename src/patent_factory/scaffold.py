"""Read-only draft builders for the versioned request inputs.

Each builder reads authoritative state (run database and/or profile database)
and emits a *draft* request object with every hash/ID binding pre-filled from
real revisions and every human-judgment field stubbed with a ``TODO(agent):``
marker. The core verbs still validate authoritatively on submission — a
scaffold only removes clerical hash-copying, never judgment.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from .config import EvaluationConfig
from .database import profile_payload
from .ideation import _profile_claim_categories, _profile_domain
from .provenance import Claim, EpistemicLabel, normalize
from .report import (
    DEFAULT_REPORT_LANGUAGE,
    REPORT_INPUT_VERSION_V2,
    REPORT_LANGUAGES,
    _current_artifact,
)

TODO = "TODO(agent): "
_FORBIDDEN_PROFILE_FIELDS = {"api_key", "credential", "email", "name", "phone", "raw_document", "secret"}


class ScaffoldError(ValueError):
    """A scaffold precondition is unmet (missing upstream state or facts)."""


def _evidence_records(connection: sqlite3.Connection, run_id: str) -> list[Mapping[str, Any]]:
    _row, research = _current_artifact(connection, run_id, "research_bundle")
    records = research.get("evidence")
    if not isinstance(records, list) or not records:
        raise ScaffoldError("scaffold requires a current research bundle with evidence — run /research first")
    return records


def _evidence_reference(record: Mapping[str, Any]) -> dict[str, Any]:
    record_json = record.get("record_json")
    normalized = json.loads(record_json) if isinstance(record_json, str) else (record_json or {})
    spans = normalized.get("excerpt_hashes") if isinstance(normalized, Mapping) else None
    span = spans[0] if isinstance(spans, list) and spans else None
    return {
        "content_hash": record["content_hash"],
        "evidence_id": record["evidence_id"],
        "limitation": None if span else "no recorded span; whole-record reference",
        "span_hash": span,
    }


def scaffold_candidate_input(
    connection: sqlite3.Connection,
    profile_connection: sqlite3.Connection,
    *,
    run_id: str,
    count: int = 3,
) -> dict[str, Any]:
    if not 1 <= count <= 12:
        raise ScaffoldError("scaffold candidate count must be between 1 and 12")
    profile = profile_payload(profile_connection)
    categories = _profile_claim_categories(profile)
    problem_ref = next(
        ({"claim_id": claim_id, "field": field, "kind": "problem"}
         for (field, claim_id), kinds in sorted(categories.items()) if "problem" in kinds),
        None,
    )
    capability_ref = next(
        ({"claim_id": claim_id, "field": field, "kind": "capability"}
         for (field, claim_id), kinds in sorted(categories.items()) if "capability" in kinds),
        None,
    )
    if problem_ref is None or capability_ref is None:
        raise ScaffoldError(
            "scaffold requires both a problem-like and a capability-like profile fact "
            "(e.g. project_summary and expertise) — enrich the profile with /setup first"
        )
    domain = _profile_domain(profile) or TODO + "technical domain"
    evidence = _evidence_records(connection, run_id)
    hypothesis = Claim(EpistemicLabel.HYPOTHESIS).as_dict()
    creative = Claim(EpistemicLabel.CREATIVE_SUGGESTION).as_dict()
    candidates = []
    for index in range(count):
        reference = _evidence_reference(evidence[index % len(evidence)])
        candidates.append({
            "claims": [
                # evidence_references bind citations to ONE field. The scaffold
                # pre-fills technical_problem and mechanism, whose report bullets
                # render citations, and leaves the hedged fields empty — the
                # renderer prints no prior-art token on a hedged bullet anyway.
                {"claim": dict(hypothesis), "evidence_references": [dict(reference)], "field": "technical_problem"},
                {"claim": dict(creative), "evidence_references": [dict(reference)], "field": "mechanism"},
                {"claim": dict(hypothesis), "evidence_references": [], "field": "expected_effects"},
                {"claim": dict(creative), "evidence_references": [], "field": "synthesis_trace"},
            ],
            "components": [TODO + "key component"],
            "domain": domain,
            "evidence_references": [reference],
            "expected_effects": [TODO + "expected measurable technical effect"],
            "implementation_example": TODO + "concrete implementation example",
            "interactions": [TODO + "how the components interact"],
            "mechanism": TODO + "proposed mechanism (modify/combine/adapt a researched one)",
            "measurable_validation": TODO + "how the effect would be measured",
            "outputs": [TODO + "output"],
            "profile_references": [dict(problem_ref), dict(capability_ref)],
            "required_inputs": [TODO + "required input"],
            "synthesis_trace": {
                "evidence_ids": [reference["evidence_id"]],
                "method": "combine",
                "narrative": TODO + "which researched mechanisms were combined/adapted, and what the ~10-30% creative delta is",
            },
            "technical_problem": TODO + "the concrete technical problem",
            "title": TODO + f"candidate title {index + 1}",
            "transformations": [TODO + "core transformation"],
            "unresolved_dependencies": [],
            "unresolved_questions": [TODO + "open question for the user"],
        })
    return {"candidates": candidates, "schema_version": "candidate-input-v1"}


def scaffold_shortlist_input(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    config: EvaluationConfig,
) -> dict[str, Any]:
    _row, candidate_set = _current_artifact(connection, run_id, "candidate_set")
    candidates = candidate_set.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ScaffoldError("scaffold requires a current candidate set — run /ideate first")
    finalists = []
    for index, candidate in enumerate(candidates[:3], start=1):
        supporting = [
            dict(reference)
            for reference in candidate.get("evidence_references", [])
            if isinstance(reference, Mapping) and reference.get("evidence_id")
        ]
        finalists.append({
            "axes": [
                {
                    "axis": axis,
                    "confidence": "medium",
                    "contrary_evidence_references": [],
                    "coverage_assessment": TODO + "what the current evidence does and does not cover",
                    "coverage_limitations": [TODO + "known coverage limitation"],
                    "gaps": [],
                    "rationale": TODO + f"why this candidate scores as it does on {axis}",
                    "rubric_version": config.rubrics[axis],
                    # A bare 0 here is why the committed golden rendered a
                    # 0/0/0 comparison matrix: `filled()`-style helpers rewrite
                    # only TODO(agent) STRINGS, so a numeric placeholder sails
                    # through untouched and evaluation.py accepts it as a real
                    # score. A sentinel that fails validation cannot be left in.
                    "score": TODO + "integer 0-100 for this axis",
                    "supporting_evidence_references": supporting,
                }
                for axis in ("differentiation", "technical_feasibility", "utility_significance")
            ],
            "candidate_id": candidate.get("candidate_id"),
            "priority": index,
            "selection_rationale": TODO + "why this candidate is a finalist",
        })
    return {
        "exclusions": [],
        "finalists": finalists,
        "insufficiency": None,
        "schema_version": "shortlist-input-v1",
    }


def scaffold_audit_query_input(
    connection: sqlite3.Connection,
    *,
    run_id: str,
) -> dict[str, Any]:
    row, finalist_set = _current_artifact(connection, run_id, "finalist_set")
    finalists = finalist_set.get("finalists")
    if not isinstance(finalists, list) or not finalists:
        raise ScaffoldError("scaffold requires a current finalist set — run /shortlist first")
    return {
        "finalist_set_hash": row["content_hash"],
        "groups": [
            {
                "finalist_id": finalist.get("finalist_id"),
                "queries": [
                    {"language": "ko", "term": TODO + "Korean search term for this finalist"},
                    {"language": "en", "term": TODO + "English search term for this finalist"},
                ],
            }
            for finalist in finalists
        ],
        "schema_version": "audit-query-input-v1",
    }


def scaffold_gate_decision_input(
    connection: sqlite3.Connection, *, run_id: str, gate_id: str,
) -> dict[str, Any]:
    """Draft a pre-filled ``gate-decision-input-v2`` for one pending checkpoint gate.

    Bindings (``gate_id``, ``subject_revision_hash``, ``approval_scope``, and
    every finalist_id/action combination the composition rule fixes) are
    clerical and pre-filled from the current gate exactly as ``gate inspect``
    reports it. Judgment fields — the top-level ``action``, ``reason``, every
    per-finalist ``feedback`` interesting/boring, the breach ``decisions``
    reasons, and the ``plan`` — are left as ``TODO(agent)`` markers: `gate
    decide` rejects any of them left unedited (core sentinel check, not this
    scaffold).
    """
    from .decisions import inspect_gate
    from .models import GateKind

    envelope = inspect_gate(connection, run_id, gate_id)
    if envelope["kind"] != GateKind.POST_AUDIT_CHECKPOINT.value:
        raise ScaffoldError("scaffold gate-decision only drafts a post_audit_checkpoint gate")
    scope = envelope["approval_scope"]
    affected = scope.get("affected_finalist_ids") or []
    bindings = scope.get("finalist_bindings") or []
    return {
        "action": TODO + "one of: approve, re_ideate, re_research, stop",
        "actor": TODO + "actor identity",
        "approval_scope": scope,
        "decisions": [
            # Only used when action=approve and breaches exist: exactly one
            # retain_with_warning entry per breaching finalist (the composition
            # rule rejects any other shape). Clear this list for every other
            # action, including approve on a clean audit.
            {
                "action": "retain_with_warning",
                "finalist_id": finalist_id,
                "reason": TODO + "why this finalist is retained despite the excessive-similarity flag",
            }
            for finalist_id in affected
        ],
        "feedback": [
            {
                "boring": TODO + "what felt less compelling about this finalist",
                "finalist_id": item["finalist_id"],
                "interesting": TODO + "what felt worth pursuing about this finalist",
            }
            for item in bindings
        ],
        "gate_id": gate_id,
        "plan": {
            # Only used when action=re_research; clear to {} for every other action.
            "needed_research": [TODO + "what to search for on an offline second pass (fixture/normalize-web/manual only)"],
        },
        "reason": TODO + "why this decision was made",
        "schema_version": "gate-decision-input-v2",
        "subject_revision_hash": envelope["subject_revision_hash"],
    }


def gate_decision_dossier(scope: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flat per-finalist audit verdict for presenting the checkpoint dossier.

    ``approval_scope.finalist_bindings`` already carries every field
    (verbatim, hash-bound); this re-projects them so an agent building the
    ``/checkpoint`` dossier does not have to reach into the nested scope blob
    by hand. ``coverage``/``upper_bound_reference_id`` matter most on a
    ``coverage_insufficient`` finalist: a null ``closest_reference_id`` there
    is not "nothing found" — it means the closest OBSERVED reference stayed
    below the excessive threshold, while ``upper_bound_reference_id`` names
    the real reference (at ``coverage``) that keeps coverage too thin to
    clear (review finding #6). This is CLI-response-only (returned via the
    `scaffold` command's `extras`, never written into the decision-input
    draft file), since `gate-decision-input-v2` rejects any extra top-level
    key.
    """
    return [{
        "closest_reference_id": item.get("closest_reference_id"),
        "coverage": item.get("coverage"),
        "finalist_id": item["finalist_id"],
        "outcome": item["outcome"],
        "r_hi": item["r_hi"], "r_obs": item["r_obs"],
        "upper_bound_reference_id": item.get("upper_bound_reference_id"),
    } for item in (scope.get("finalist_bindings") or [])]


def scaffold_report_input(
    profile_connection: sqlite3.Connection,
    *,
    language: str = DEFAULT_REPORT_LANGUAGE,
) -> dict[str, Any]:
    if language not in REPORT_LANGUAGES:
        raise ScaffoldError("scaffold report language must be en or ko")
    profile = profile_payload(profile_connection)
    facts = profile.get("facts")
    fields = sorted(
        field for field in (facts or {})
        if normalize(str(field)).casefold() not in _FORBIDDEN_PROFILE_FIELDS
    )
    if not fields:
        raise ScaffoldError("scaffold requires renderable technical profile fields — run /setup first")
    return {
        "drafter": {"id": TODO + "drafter id", "pass_id": TODO + "draft pass id", "type": "agent"},
        "handoff_questions": [TODO + "question for the patent attorney"],
        "language": language,
        "profile_fields": fields,
        "recommended_investigations": [TODO + "recommended follow-up investigation"],
        "report_date": TODO + "YYYY-MM-DD",
        "revision": None,
        "schema_version": REPORT_INPUT_VERSION_V2,
        "sensitive_disclosures": [],
    }


def evidence_binding_table(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Per-evidence hash table echoed to the agent for later label upgrades."""

    table = []
    for record in _evidence_records(connection, run_id):
        record_json = record.get("record_json")
        normalized = json.loads(record_json) if isinstance(record_json, str) else (record_json or {})
        spans = normalized.get("excerpt_hashes") if isinstance(normalized, Mapping) else None
        table.append({
            "content_hash": record.get("content_hash"),
            "evidence_id": record.get("evidence_id"),
            "excerpt_hashes": list(spans) if isinstance(spans, list) else [],
            "title": record.get("title"),
        })
    return table


def count_todos(value: Any) -> int:
    # Shares its detection rule with core's `_reject_todo_marker`
    # (`decisions.contains_todo_marker`) — a `startswith(TODO)` check here
    # used to disagree with core's substring match, so an edited field that
    # still MENTIONED "TODO(agent)" anywhere reported 0 here but was
    # rejected there anyway (finding #15).
    if isinstance(value, str):
        from .decisions import contains_todo_marker

        return 1 if contains_todo_marker(value) else 0
    if isinstance(value, Mapping):
        return sum(count_todos(item) for item in value.values())
    if isinstance(value, list):
        return sum(count_todos(item) for item in value)
    return 0


def scaffold_feature_map_input(
    connection: sqlite3.Connection, *, run_id: str, config: Any,
) -> dict[str, Any]:
    """Draft a feature-map request with every derivable binding pre-filled.

    Three things here are pure clerical derivation and cannot be authored by
    hand from CLI output:

    * ``candidate_span_hashes`` — ``digest({"field": f, "text": normalize(t)})``
      over ``FEATURE_SOURCE_FIELDS``. An agent that does not emit these has to
      reverse-engineer this repo's canonicalization (NFKC, strip, compact
      separators, sorted keys) and finds out only via a generic "candidate span
      does not belong to the finalist revision".
    * ``weight``/``essential`` — ``weight`` must sum EXACTLY to the configured
      category weights, so a wrong guess is rejected outright.
    * the three artifact hashes.

    What it deliberately does NOT do:

    * It does not pre-fill ``reference_span_hashes``. Choosing which retained
      span justifies a decision is the reviewer's judgment. Instead each
      decision carries ``available_reference_span_hashes`` — the menu — with the
      choice left empty. Enumerating options is clerical; choosing is not.
    * It does not emit the frozen ``review`` attestation. A tool must never
      manufacture the record that a human review occurred.
    * It does not compute ``map_id``: that digests the FILLED map, so it only
      exists after the judgment fields are written. Seal it afterwards with
      ``scaffold feature-map --seal``.
    """

    # Imported inside the function, like feature_map_id below: audit imports the
    # report/state layers this module also imports.
    from .audit import _candidate_span_hashes

    finalist_row, finalist_set = _current_artifact(connection, run_id, "finalist_set")
    corpus_row, corpus_set = _current_artifact(connection, run_id, "corpus_set")
    _candidate_row, candidate_set = _current_artifact(connection, run_id, "candidate_set")
    finalists = finalist_set.get("finalists") or []
    if not finalists:
        raise ScaffoldError("scaffold requires a current finalist set — run /shortlist first")
    candidates = {item["candidate_id"]: item for item in candidate_set.get("candidates", [])}
    corpora = {item["finalist_id"]: item for item in corpus_set.get("corpora", [])}
    weights = dict(config.feature_weights)

    maps = []
    for finalist in finalists:
        finalist_id = finalist.get("finalist_id")
        corpus = corpora.get(finalist_id)
        if corpus is None:
            raise ScaffoldError(f"corpus set has no entry for {finalist_id} — run /audit retrieve first")
        candidate = candidates.get(finalist.get("candidate_id"), {})
        features = []
        for category in sorted(weights):
            spans = sorted(_candidate_span_hashes(candidate, category))
            features.append({
                "candidate_span_hashes": spans,
                "category": category,
                "description": TODO + f"what the {category} feature actually is, in the inventor's terms",
                "essential": True,
                "feature_id": f"feature-{category}",
                "weight": weights[category],
            })
        reference_maps = []
        for record in corpus.get("records", []):
            span_menu = sorted((record.get("record") or {}).get("field_span_hashes", {}).values())
            inspected = sorted(
                field for field, value in (record.get("record") or {}).items()
                if field in {"title", "abstract", "classifications"} and value
            )
            reference_maps.append({
                "decisions": [{
                    "available_reference_span_hashes": span_menu,
                    "feature_id": feature["feature_id"],
                    "rationale": TODO + "why this reference does or does not disclose the feature",
                    "reference_span_hashes": [],
                    "status": TODO + "one of: matched, different, not_disclosed, unknown",
                } for feature in features],
                "evidence_id": record.get("evidence_id"),
                "inspected_fields": inspected,
            })
        maps.append({
            "feature_map": {
                "candidate_classifications": sorted(candidate.get("classifications", []) or []),
                "features": features,
                "reference_maps": reference_maps,
                "review": TODO + (
                    "replace with a review block once a human has actually reviewed this map; "
                    "a scaffold must not assert that a review happened"
                ),
            },
            "finalist_id": finalist_id,
            "map_id": TODO + "derive with: scaffold feature-map --seal, after filling every field",
        })
    return {
        "corpus_set_hash": corpus_row["content_hash"],
        "finalist_set_hash": finalist_row["content_hash"],
        "maps": maps,
        "schema_version": "feature-map-set-input-v1",
    }


def _strip_scaffold_only_keys(feature_map: Any) -> Any:
    """Drop the pick-list menu the generator added, before validation sees it.

    ``scaffold feature-map`` writes ``available_reference_span_hashes`` into each
    decision so the agent can choose ``reference_span_hashes`` from it. That key
    is tool-authored scaffolding, not a judgment field — but
    ``canonical_feature_map`` demands each decision be *exactly*
    ``{feature_id, rationale, reference_span_hashes, status}`` and rejects the
    extra key. Removing it is clerical, so ``--seal`` does it rather than making
    the agent hand-delete a field the tool itself added.
    """

    if not isinstance(feature_map, Mapping):
        return feature_map
    result = dict(feature_map)
    reference_maps = []
    for reference in result.get("reference_maps", []) or []:
        if not isinstance(reference, Mapping):
            reference_maps.append(reference)
            continue
        cleaned = dict(reference)
        cleaned["decisions"] = [
            {key: value for key, value in decision.items() if key != "available_reference_span_hashes"}
            if isinstance(decision, Mapping) else decision
            for decision in (cleaned.get("decisions") or [])
        ]
        reference_maps.append(cleaned)
    if "reference_maps" in result:
        result["reference_maps"] = reference_maps
    return result


def seal_feature_map_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute every ``map_id`` from the map it is supposed to bind.

    ``map_id`` digests the *canonicalized filled* map, so it changes the moment an
    agent writes a ``status`` or a ``rationale`` — which means it cannot be
    pre-filled by a scaffold, and ``audit score`` rejects a stale one outright
    (``audit.run_audit_scoring``: "map identity does not bind frozen content").
    Sealing is the last clerical step before submission: it re-derives the
    identity, it never edits a judgment field.

    Validation stays authoritative in ``audit``; this only refuses shapes it
    cannot seal correctly.
    """

    from .audit import feature_map_id

    if not isinstance(payload, Mapping):
        raise ScaffoldError("seal: feature-map-set-input-v1 object required")
    if payload.get("schema_version") != "feature-map-set-input-v1":
        raise ScaffoldError("seal: schema_version must be feature-map-set-input-v1")
    maps = payload.get("maps")
    if not isinstance(maps, list) or not maps:
        raise ScaffoldError("seal: maps must be a non-empty list")
    sealed_maps = []
    for index, item in enumerate(maps):
        if not isinstance(item, Mapping) or "feature_map" not in item or "finalist_id" not in item:
            raise ScaffoldError(f"seal: maps[{index}] needs feature_map and finalist_id")
        remaining = set(item) - {"feature_map", "finalist_id", "map_id"}
        if remaining:
            raise ScaffoldError(f"seal: maps[{index}] has unexpected fields: {', '.join(sorted(remaining))}")
        feature_map = _strip_scaffold_only_keys(item["feature_map"])
        if count_todos(feature_map):
            raise ScaffoldError(
                f"seal: maps[{index}] still contains TODO(agent) markers; fill every judgment field first"
            )
        sealed_maps.append({
            "feature_map": feature_map,
            "finalist_id": item["finalist_id"],
            "map_id": feature_map_id(item["finalist_id"], feature_map),
        })
    return {**dict(payload), "maps": sealed_maps}


__all__ = [
    "ScaffoldError", "TODO", "count_todos", "evidence_binding_table", "gate_decision_dossier",
    "scaffold_audit_query_input", "scaffold_candidate_input", "scaffold_gate_decision_input",
    "scaffold_report_input", "scaffold_feature_map_input", "scaffold_shortlist_input", "seal_feature_map_input",
]
