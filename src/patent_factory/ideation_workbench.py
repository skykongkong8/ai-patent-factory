"""Divergence-first ideation workbench scaffolding and read-only validation."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import EvaluationConfig
from .database import profile_payload
from .ideation import _current_artifact, _profile_claim_categories, validate_candidate_input
from .lint import workbench_advisories
from .paths import PathPolicyError, owner_only_file
from .provenance import digest, normalize, strict_json_loads
from .scaffold import TODO
from .state import StateError

BRIEF_SCHEMA = "ideation-brief-v1"
IDEA_SCHEMA = "idea-node-v1"
RELATION_SCHEMA = "idea-relation-v1"
CLUSTER_SCHEMA = "idea-clusters-v1"
LINEAGE_SCHEMA = "idea-lineage-v1"
WORKBENCH_SCHEMA = "ideation-workbench-validation-v1"
DIRECTIONAL_RELATIONS = {"combines", "derives", "revises"}
RELATION_TYPES = DIRECTIONAL_RELATIONS | {"contrasts", "parks"}
_FORBIDDEN_PROFILE_FIELDS = {"api_key", "credential", "email", "name", "phone", "raw_document", "secret"}


class WorkbenchValidationError(ValueError):
    """A workbench stage file is malformed or not ready for the requested stage."""


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkbenchValidationError(f"{path}: object required")
    return value


def _exact_fields(value: Mapping[str, Any], fields: set[str], path: str) -> None:
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown:
        raise WorkbenchValidationError(f"{path}: unknown fields: {', '.join(unknown)}")
    if missing:
        raise WorkbenchValidationError(f"{path}: missing fields: {', '.join(missing)}")


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or "TODO(agent)" in value:
        raise WorkbenchValidationError(f"{path}: filled string required")
    return value


def _hash(value: Any, path: str) -> str:
    value = _text(value, path)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise WorkbenchValidationError(f"{path}: canonical sha256 hex required")
    return value


def _texts(value: Any, path: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise WorkbenchValidationError(f"{path}: array required")
    result = tuple(_text(item, f"{path}[{index}]") for index, item in enumerate(value))
    if not result and not allow_empty:
        raise WorkbenchValidationError(f"{path}: non-empty array required")
    if len(set(result)) != len(result):
        raise WorkbenchValidationError(f"{path}: duplicate values are not allowed")
    return result


def _read_json(path: Path, byte_budget: int, label: str) -> Mapping[str, Any]:
    if not 1 <= byte_budget <= 10_000_000:
        raise WorkbenchValidationError(f"{label} byte budget must be between 1 and 10000000")
    if path.stat().st_size > byte_budget:
        raise WorkbenchValidationError(f"{label} exceeds byte budget")
    payload = strict_json_loads(path.read_bytes())
    return _object(payload, label)


def _read_jsonl(path: Path, byte_budget: int, label: str) -> list[Mapping[str, Any]]:
    if not 1 <= byte_budget <= 10_000_000:
        raise WorkbenchValidationError(f"{label} byte budget must be between 1 and 10000000")
    if path.stat().st_size > byte_budget:
        raise WorkbenchValidationError(f"{label} exceeds byte budget")
    rows = []
    total = 0
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        total += len(line.encode("utf-8"))
        if total > byte_budget:
            raise WorkbenchValidationError(f"{label} exceeds byte budget")
        rows.append(_object(strict_json_loads(line.encode("utf-8")), f"{label}[{index}]"))
    return rows


def _reject_todos(value: Any, path: str) -> None:
    if isinstance(value, str):
        if "TODO(agent)" in value:
            raise WorkbenchValidationError(f"{path}: TODO(agent) placeholder is unfilled")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_todos(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_todos(item, f"{path}[{index}]")


def _safe_existing_file(path: Path, label: str) -> None:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise PathPolicyError(f"{label} rejected: regular file required")


def _safe_existing_dir(path: Path, label: str) -> None:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise PathPolicyError(f"{label} rejected: directory required")


def _mkdir_private(path: Path) -> None:
    if path.exists() or path.is_symlink():
        _safe_existing_dir(path, "ideation workbench directory")
    else:
        parent = path.parent
        if parent != path and not parent.exists():
            _mkdir_private(parent)
        path.mkdir(mode=0o700)
    os.chmod(path, 0o700, follow_symlinks=False)


def _evidence_card(record: Mapping[str, Any]) -> dict[str, Any]:
    record_json = record.get("record_json")
    normalized = json.loads(record_json) if isinstance(record_json, str) else (record_json or {})
    excerpts = normalized.get("excerpt_hashes") if isinstance(normalized, Mapping) else []
    spans = normalized.get("field_span_hashes") if isinstance(normalized, Mapping) else {}
    metadata = {
        "canonical_url": record.get("canonical_url"),
        "coverage": record.get("coverage"),
        "created_at": record.get("created_at"),
        "original_identifier": record.get("original_identifier") or record.get("source_locator"),
        "provenance": record.get("provenance"),
        "query": record.get("query"),
        "title": record.get("title"),
    }
    return normalize({
        "content_hash": record.get("content_hash"),
        "evidence_id": record.get("evidence_id"),
        "excerpt_hashes": excerpts if isinstance(excerpts, list) else [],
        "language": record.get("language"),
        "metadata_hash": digest(metadata),
        "record_hash": digest(normalized),
        "semantic_status": "hash_only_private_boundary",
        "source_type": record.get("source_type"),
        "span_hashes": sorted(str(value) for value in spans.values())
        if isinstance(spans, Mapping) else [],
    })


def _profile_cards(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    categories = _profile_claim_categories(profile)
    facts = profile.get("facts") if isinstance(profile.get("facts"), Mapping) else {}
    cards = []
    for field, entry in sorted(facts.items()):
        if str(field).casefold() in _FORBIDDEN_PROFILE_FIELDS or not isinstance(entry, Mapping):
            continue
        claim_ids = [claim.get("claim_id") for claim in entry.get("claims", []) if isinstance(claim, Mapping)]
        kinds = sorted({kind for claim_id in claim_ids for kind in categories.get((str(field), str(claim_id)), ())})
        if kinds:
            cards.append({
                "claim_ids": [claim_id for claim_id in claim_ids if claim_id],
                "field": field,
                "kinds": kinds,
                "value_hash": digest(entry.get("value", "")),
            })
    return cards


def _current_reideate_seed(connection, run_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT ar.* FROM artifact_revisions ar JOIN current_artifacts ca "
        "ON ca.revision_id=ar.revision_id "
        "WHERE ar.run_id=? AND ca.kind='gate_resolution' AND ar.stale=0",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    content = json.loads(row["content_json"])
    if content.get("action") != "re_ideate" or content.get("gate_kind") != "post_audit_checkpoint":
        return None
    envelope = connection.execute(
        "SELECT approval_scope_json,approval_scope_hash FROM gate_envelopes "
        "WHERE run_id=? AND gate_id=?",
        (run_id, content.get("gate_id")),
    ).fetchone()
    if envelope is None:
        raise StateError("current re_ideate resolution is missing its gate envelope")
    scope = json.loads(envelope["approval_scope_json"])
    if content.get("approval_scope_hash") != envelope["approval_scope_hash"]:
        raise StateError("current re_ideate resolution approval scope is inconsistent")
    bindings = scope.get("finalist_bindings", [])
    if not isinstance(bindings, list):
        raise StateError("current re_ideate resolution finalist bindings are malformed")
    feedback = content.get("feedback", [])
    if not isinstance(feedback, list):
        raise StateError("current re_ideate resolution feedback is malformed")
    return normalize({
        "action": "re_ideate",
        "approval_scope_hash": envelope["approval_scope_hash"],
        "feedback_bindings": [
            {
                "feedback_hash": digest({
                    "boring": item.get("boring"),
                    "interesting": item.get("interesting"),
                }),
                "finalist_id": item.get("finalist_id"),
            }
            for item in feedback if isinstance(item, Mapping)
        ],
        "finalist_bindings": [
            {
                "candidate_id": item.get("candidate_id"),
                "finalist_id": item.get("finalist_id"),
            }
            for item in bindings if isinstance(item, Mapping)
        ],
        "gate_id": content.get("gate_id"),
        "resolution_hash": row["content_hash"],
        "resolution_revision_id": row["revision_id"],
        "subject_revision_hash": content.get("subject_revision_hash"),
    })


def scaffold_ideation_brief(
    connection, profile_connection, *, run_id: str, config: EvaluationConfig,
    reideate_seed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    profile = profile_payload(profile_connection)
    research = _current_artifact(connection, run_id, "research_bundle")
    evidence = research.content.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise StateError("current research_bundle evidence is required")
    cards = _profile_cards(profile)
    problem_cards = [card for card in cards if "problem" in card["kinds"]]
    capability_cards = [card for card in cards if "capability" in card["kinds"]]
    seed = _current_reideate_seed(connection, run_id) if reideate_seed is None else normalize({
        "provided_seed_hash": digest(reideate_seed),
    })
    body = normalize({
        "data_classes": ["profile_fact_hashes", "research_evidence_hashes", "agent_ideation_notes"],
        "egress": {
            "allowed": False,
            "recipient": None,
            "scope": "local_cli_only",
            "semantic_content_available": False,
        },
        "evidence_cards": [_evidence_card(record) for record in evidence],
        "evaluation_config_hash": config.content_hash,
        "profile": {"capability_cards": capability_cards, "problem_cards": problem_cards},
        "profile_revision_hash": digest(profile),
        "profile_revision_id": profile.get("profile_revision"),
        "research_revision_hash": research.content_hash,
        "research_revision_id": research.revision_id,
        "reideate_seed": seed,
        "run_id": run_id,
        "semantic_gaps": [
            digest(item) for item in research.content.get("coverage_limitations", [])
        ] if isinstance(research.content.get("coverage_limitations", []), list) else [],
        "version": BRIEF_SCHEMA,
    })
    return {**body, "brief_id": "ib_" + digest(body)[:20]}


def initialize_workbench(out_path: Path, brief: Mapping[str, Any]) -> dict[str, Any]:
    root = out_path.parent
    if out_path.name != "brief-v1.json":
        raise WorkbenchValidationError("ideation workbench output must be named brief-v1.json")
    _mkdir_private(root)
    for child in ("sessions", "promoted"):
        _mkdir_private(root / child)
    history_root = root / "history"
    if history_root.exists() or history_root.is_symlink():
        _safe_existing_dir(history_root, "ideation history")
    notes = root / "notes.md"
    if not notes.exists():
        notes.write_text("# Ideation notes\n", encoding="utf-8")
        owner_only_file(notes)
    elif notes.is_symlink() or not notes.is_file():
        raise PathPolicyError("ideation notes rejected: regular file required")
    encoded = json.dumps(brief, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if out_path.exists():
        _safe_existing_file(out_path, "ideation brief")
        existing = out_path.read_text(encoding="utf-8")
        if existing == encoded:
            owner_only_file(out_path)
            return {
                "brief_id": brief["brief_id"],
                "brief_hash": digest(brief),
                "replayed": True,
                "workbench_root": root.as_posix(),
            }
        old_hash = digest(strict_json_loads(existing.encode("utf-8")))
        _mkdir_private(history_root)
        history = history_root / f"brief-{old_hash}.json"
        if history.exists() or history.is_symlink():
            _safe_existing_file(history, "ideation history brief")
        else:
            history.write_text(existing, encoding="utf-8")
            owner_only_file(history)
        if digest(strict_json_loads(history.read_bytes())) != old_hash:
            raise WorkbenchValidationError("ideation history: archived brief hash mismatch")
    out_path.write_text(encoded, encoding="utf-8")
    owner_only_file(out_path)
    return {
        "brief_id": brief["brief_id"],
        "brief_hash": digest(brief),
        "replayed": False,
        "workbench_root": root.as_posix(),
    }


def _validate_brief_shape(brief: Mapping[str, Any], path: str) -> None:
    fields = {
        "brief_id", "data_classes", "egress", "evaluation_config_hash", "evidence_cards",
        "profile", "profile_revision_hash", "profile_revision_id", "research_revision_hash",
        "research_revision_id", "reideate_seed", "run_id", "semantic_gaps", "version",
    }
    _exact_fields(brief, fields, path)
    if brief.get("version") != BRIEF_SCHEMA:
        raise WorkbenchValidationError(f"{path}.version: {BRIEF_SCHEMA} required")
    for field in ("brief_id", "profile_revision_id", "research_revision_id", "run_id"):
        _text(brief.get(field), f"{path}.{field}")
    for field in ("evaluation_config_hash", "profile_revision_hash", "research_revision_hash"):
        _hash(brief.get(field), f"{path}.{field}")
    body = dict(brief)
    brief_id = body.pop("brief_id")
    if brief_id != "ib_" + digest(body)[:20]:
        raise WorkbenchValidationError(f"{path}.brief_id: content binding mismatch")
    data_classes = _texts(brief.get("data_classes"), f"{path}.data_classes")
    if set(data_classes) != {
        "agent_ideation_notes", "profile_fact_hashes", "research_evidence_hashes",
    }:
        raise WorkbenchValidationError(f"{path}.data_classes: unsupported data classes")
    egress = _object(brief.get("egress"), f"{path}.egress")
    _exact_fields(
        egress, {"allowed", "recipient", "scope", "semantic_content_available"},
        f"{path}.egress",
    )
    if egress != {
        "allowed": False,
        "recipient": None,
        "scope": "local_cli_only",
        "semantic_content_available": False,
    }:
        raise WorkbenchValidationError(f"{path}.egress: private boundary must remain closed")
    profile = _object(brief.get("profile"), f"{path}.profile")
    _exact_fields(profile, {"capability_cards", "problem_cards"}, f"{path}.profile")
    for category, kind in (("capability_cards", "capability"), ("problem_cards", "problem")):
        cards = profile.get(category)
        if not isinstance(cards, list):
            raise WorkbenchValidationError(f"{path}.profile.{category}: array required")
        for index, raw in enumerate(cards):
            card = _object(raw, f"{path}.profile.{category}[{index}]")
            _exact_fields(card, {"claim_ids", "field", "kinds", "value_hash"}, f"{path}.profile.{category}[{index}]")
            _text(card.get("field"), f"{path}.profile.{category}[{index}].field")
            _hash(card.get("value_hash"), f"{path}.profile.{category}[{index}].value_hash")
            claim_ids = _texts(card.get("claim_ids"), f"{path}.profile.{category}[{index}].claim_ids")
            kinds = _texts(card.get("kinds"), f"{path}.profile.{category}[{index}].kinds")
            if not claim_ids or kind not in kinds or not set(kinds).issubset({"problem", "capability"}):
                raise WorkbenchValidationError(f"{path}.profile.{category}[{index}]: category binding mismatch")
    evidence = brief.get("evidence_cards")
    if not isinstance(evidence, list) or not evidence:
        raise WorkbenchValidationError(f"{path}.evidence_cards: non-empty array required")
    evidence_ids: set[str] = set()
    evidence_fields = {
        "content_hash", "evidence_id", "excerpt_hashes", "language", "metadata_hash",
        "record_hash", "semantic_status", "source_type", "span_hashes",
    }
    for index, raw in enumerate(evidence):
        card = _object(raw, f"{path}.evidence_cards[{index}]")
        _exact_fields(card, evidence_fields, f"{path}.evidence_cards[{index}]")
        evidence_id = _text(card.get("evidence_id"), f"{path}.evidence_cards[{index}].evidence_id")
        if evidence_id in evidence_ids:
            raise WorkbenchValidationError(f"{path}.evidence_cards: duplicate evidence_id")
        evidence_ids.add(evidence_id)
        for field in ("content_hash", "metadata_hash", "record_hash"):
            _hash(card.get(field), f"{path}.evidence_cards[{index}].{field}")
        for field in ("excerpt_hashes", "span_hashes"):
            for hash_index, hash_value in enumerate(_texts(
                card.get(field), f"{path}.evidence_cards[{index}].{field}", allow_empty=True,
            )):
                _hash(hash_value, f"{path}.evidence_cards[{index}].{field}[{hash_index}]")
        if card.get("semantic_status") != "hash_only_private_boundary":
            raise WorkbenchValidationError(f"{path}.evidence_cards[{index}].semantic_status: hash-only boundary required")
    gaps = brief.get("semantic_gaps")
    for index, gap_hash in enumerate(_texts(gaps, f"{path}.semantic_gaps", allow_empty=True)):
        _hash(gap_hash, f"{path}.semantic_gaps[{index}]")
    seed = brief.get("reideate_seed")
    if seed is not None:
        seed = _object(seed, f"{path}.reideate_seed")
        if set(seed) == {"provided_seed_hash"}:
            _hash(seed.get("provided_seed_hash"), f"{path}.reideate_seed.provided_seed_hash")
        else:
            _exact_fields(
                seed,
                {
                    "action", "approval_scope_hash", "feedback_bindings", "finalist_bindings",
                    "gate_id", "resolution_hash", "resolution_revision_id", "subject_revision_hash",
                },
                f"{path}.reideate_seed",
            )
            if seed.get("action") != "re_ideate":
                raise WorkbenchValidationError(f"{path}.reideate_seed.action: re_ideate required")
            for field in ("gate_id", "resolution_revision_id"):
                _text(seed.get(field), f"{path}.reideate_seed.{field}")
            for field in ("approval_scope_hash", "resolution_hash", "subject_revision_hash"):
                _hash(seed.get(field), f"{path}.reideate_seed.{field}")
            for collection, required in (
                ("feedback_bindings", {"feedback_hash", "finalist_id"}),
                ("finalist_bindings", {"candidate_id", "finalist_id"}),
            ):
                values = seed.get(collection)
                if not isinstance(values, list) or not values:
                    raise WorkbenchValidationError(f"{path}.reideate_seed.{collection}: non-empty array required")
                seen = set()
                for index, raw in enumerate(values):
                    item = _object(raw, f"{path}.reideate_seed.{collection}[{index}]")
                    _exact_fields(item, required, f"{path}.reideate_seed.{collection}[{index}]")
                    for field in required:
                        _text(item.get(field), f"{path}.reideate_seed.{collection}[{index}].{field}")
                    if "feedback_hash" in required:
                        _hash(item.get("feedback_hash"), f"{path}.reideate_seed.{collection}[{index}].feedback_hash")
                    finalist_id = item["finalist_id"]
                    if finalist_id in seen:
                        raise WorkbenchValidationError(f"{path}.reideate_seed.{collection}: duplicate finalist_id")
                    seen.add(finalist_id)


def _briefs(root: Path, byte_budget: int) -> tuple[Mapping[str, Any], dict[str, Mapping[str, Any]]]:
    path = root / "brief-v1.json"
    _safe_existing_file(path, "ideation brief")
    current = _read_json(path, byte_budget, "ideation brief")
    _validate_brief_shape(current, "ideation brief")
    known = {digest(current): current}
    history_root = root / "history"
    if history_root.exists() or history_root.is_symlink():
        _safe_existing_dir(history_root, "ideation history")
        for history_path in sorted(history_root.iterdir()):
            _safe_existing_file(history_path, "ideation history brief")
            archived = _read_json(history_path, byte_budget, "ideation history brief")
            _validate_brief_shape(archived, "ideation history brief")
            archived_hash = digest(archived)
            if history_path.name != f"brief-{archived_hash}.json":
                raise WorkbenchValidationError("ideation history: filename/hash binding mismatch")
            if archived_hash in known and known[archived_hash] != archived:
                raise WorkbenchValidationError("ideation history: duplicate hash conflict")
            known[archived_hash] = archived
    return current, known


def _profile_reference_identities(brief: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    identities: set[tuple[str, str, str]] = set()
    profile = brief.get("profile", {})
    if not isinstance(profile, Mapping):
        return identities
    for category in ("problem_cards", "capability_cards"):
        cards = profile.get(category, [])
        if not isinstance(cards, list):
            continue
        for card in cards:
            if not isinstance(card, Mapping):
                continue
            for claim_id in card.get("claim_ids", []):
                for kind in card.get("kinds", []):
                    identities.add((str(card.get("field")), str(claim_id), str(kind)))
    return identities


def _ideas(
    root: Path, briefs: Mapping[str, Mapping[str, Any]], byte_budget: int,
) -> dict[str, Mapping[str, Any]]:
    sessions = root / "sessions"
    _safe_existing_dir(sessions, "ideation sessions")
    ideas: dict[str, Mapping[str, Any]] = {}
    session_paths = []
    for path in sorted(sessions.iterdir()):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise PathPolicyError("ideation session rejected: directory required")
        if stat.S_ISDIR(mode):
            session_paths.append(path)
        else:
            raise PathPolicyError("ideation session rejected: directory required")
    if not session_paths:
        raise WorkbenchValidationError("ideation sessions: at least one divergence session required")
    for session in session_paths:
        path = session / "ideas.jsonl"
        _safe_existing_file(path, f"{session.name}/ideas.jsonl")
        for index, item in enumerate(_read_jsonl(path, byte_budget, f"{session.name}/ideas.jsonl")):
            idea_path = f"{session.name}/ideas.jsonl[{index}]"
            _exact_fields(item, {
                "brief_hash", "creative_status", "evidence_ids", "idea_id", "inputs", "lens",
                "limitations", "outputs", "profile_references", "rough_mechanism",
                "schema_version", "session_id", "technical_problem", "title", "transformations",
                "validation_approach",
            }, idea_path)
            if item.get("schema_version") != IDEA_SCHEMA:
                raise WorkbenchValidationError(f"{idea_path}.schema_version: {IDEA_SCHEMA} required")
            item_id = _text(item.get("idea_id"), f"ideas[{index}].idea_id")
            if item_id.startswith("ca_"):
                raise WorkbenchValidationError(f"ideas[{index}].idea_id: raw workbench ID required")
            if item_id in ideas:
                raise WorkbenchValidationError("ideas: duplicate idea_id")
            if item.get("session_id") != session.name:
                raise WorkbenchValidationError(f"{item_id}: session_id binding mismatch")
            brief_hash = _hash(item.get("brief_hash"), f"{item_id}.brief_hash")
            bound_brief = briefs.get(brief_hash)
            if bound_brief is None:
                raise WorkbenchValidationError(f"{item_id}: unknown current/history brief_hash")
            for field in ("lens", "title", "technical_problem", "rough_mechanism", "validation_approach"):
                _text(item.get(field), f"{item_id}.{field}")
            for field in ("inputs", "transformations", "outputs"):
                _texts(item.get(field), f"{item_id}.{field}")
            _texts(item.get("limitations"), f"{item_id}.limitations", allow_empty=True)
            if item.get("creative_status") != "creative_suggestion":
                raise WorkbenchValidationError(f"{item_id}.creative_status: creative_suggestion required")
            refs = set(_texts(item.get("evidence_ids"), f"{item_id}.evidence_ids", allow_empty=True))
            evidence_ids = {
                card.get("evidence_id") for card in bound_brief.get("evidence_cards", [])
                if isinstance(card, Mapping)
            }
            if not refs.issubset(evidence_ids):
                raise WorkbenchValidationError(f"{item_id}.evidence_ids: unknown brief evidence")
            raw_profile_references = item.get("profile_references")
            if not isinstance(raw_profile_references, list):
                raise WorkbenchValidationError(f"{item_id}.profile_references: array required")
            known_profile_references = _profile_reference_identities(bound_brief)
            seen_profile_references: set[tuple[str, str, str]] = set()
            for reference_index, raw in enumerate(raw_profile_references):
                reference = _object(raw, f"{item_id}.profile_references[{reference_index}]")
                _exact_fields(reference, {"claim_id", "field", "kind"}, f"{item_id}.profile_references[{reference_index}]")
                identity = tuple(
                    _text(reference.get(field), f"{item_id}.profile_references[{reference_index}].{field}")
                    for field in ("field", "claim_id", "kind")
                )
                if identity in seen_profile_references:
                    raise WorkbenchValidationError(f"{item_id}.profile_references: duplicate reference")
                seen_profile_references.add(identity)
                if identity not in known_profile_references:
                    raise WorkbenchValidationError(f"{item_id}.profile_references: unknown brief profile claim")
            ideas[item_id] = item
    return ideas


def _relations(
    root: Path, ideas: Mapping[str, Mapping[str, Any]], byte_budget: int,
    briefs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    relations: dict[str, Mapping[str, Any]] = {}
    graph: dict[str, set[str]] = {idea_id: set() for idea_id in ideas}
    for path in sorted((root / "sessions").glob("*/relations.jsonl")):
        _safe_existing_file(path, path.name)
        for index, item in enumerate(_read_jsonl(path, byte_budget, path.name)):
            relation_path = f"{path.parent.name}/relations.jsonl[{index}]"
            _exact_fields(item, {
                "brief_hash", "rationale", "relation_id", "schema_version", "session_id",
                "source_idea_ids", "target_idea_ids", "type",
            }, relation_path)
            if item.get("schema_version") != RELATION_SCHEMA:
                raise WorkbenchValidationError(f"relation.schema_version: {RELATION_SCHEMA} required")
            if item.get("brief_hash") not in briefs:
                raise WorkbenchValidationError("relation.brief_hash: known current/history brief hash required")
            if item.get("session_id") != path.parent.name:
                raise WorkbenchValidationError("relation.session_id: directory binding mismatch")
            relation_id = _text(item.get("relation_id"), "relation.relation_id")
            if relation_id in relations:
                raise WorkbenchValidationError("relations: duplicate relation_id")
            relation_type = _text(item.get("type"), f"{relation_id}.type")
            if relation_type not in RELATION_TYPES:
                raise WorkbenchValidationError(f"{relation_id}.type: unsupported relation")
            source_ids = _texts(item.get("source_idea_ids"), f"{relation_id}.source_idea_ids")
            target_ids = _texts(item.get("target_idea_ids"), f"{relation_id}.target_idea_ids", allow_empty=True)
            known = set(source_ids) | set(target_ids)
            if not known.issubset(ideas) or set(source_ids) & set(target_ids):
                raise WorkbenchValidationError(f"{relation_id}: unknown or self-referential idea")
            if relation_type == "combines" and (len(source_ids) < 2 or len(target_ids) != 1):
                raise WorkbenchValidationError(f"{relation_id}: combines requires at least two sources and one target")
            if relation_type in {"derives", "revises"} and (len(source_ids) != 1 or len(target_ids) != 1):
                raise WorkbenchValidationError(f"{relation_id}: {relation_type} requires one source and one target")
            if relation_type == "contrasts" and (len(source_ids) != 2 or target_ids):
                raise WorkbenchValidationError(f"{relation_id}: contrasts requires exactly two sources and no target")
            if relation_type == "parks" and (len(source_ids) != 1 or target_ids):
                raise WorkbenchValidationError(f"{relation_id}: parks requires one source and no target")
            _text(item.get("rationale"), f"{relation_id}.rationale")
            if relation_type in DIRECTIONAL_RELATIONS:
                for source_id in source_ids:
                    for target_id in target_ids:
                        graph[source_id].add(target_id)
            relations[relation_id] = item
    if not relations:
        raise WorkbenchValidationError("relations: at least one relation required for entangle")
    _assert_acyclic(graph)
    return relations


def _assert_acyclic(graph: Mapping[str, set[str]]) -> None:
    temporary: set[str] = set()
    permanent: set[str] = set()

    def visit(node: str) -> None:
        if node in permanent:
            return
        if node in temporary:
            raise WorkbenchValidationError("relations: directional combines/derives/revises graph must be acyclic")
        temporary.add(node)
        for child in graph.get(node, set()):
            visit(child)
        temporary.remove(node)
        permanent.add(node)

    for node in graph:
        visit(node)


def _clusters(
    root: Path, ideas: Mapping[str, Mapping[str, Any]], byte_budget: int,
    briefs: Mapping[str, Mapping[str, Any]],
) -> int:
    count = 0
    cluster_ids: set[str] = set()
    for path in sorted((root / "sessions").glob("*/clusters-v1.json")):
        _safe_existing_file(path, path.name)
        payload = _read_json(path, byte_budget, path.name)
        _exact_fields(payload, {"brief_hash", "clusters", "schema_version", "session_id"}, path.name)
        if payload.get("schema_version") != CLUSTER_SCHEMA or not isinstance(payload.get("clusters"), list):
            raise WorkbenchValidationError(f"{path.name}: {CLUSTER_SCHEMA} required")
        if payload.get("brief_hash") not in briefs:
            raise WorkbenchValidationError(f"{path.name}: brief_hash binding mismatch")
        if payload.get("session_id") != path.parent.name:
            raise WorkbenchValidationError(f"{path.name}: session_id binding mismatch")
        for item in payload["clusters"]:
            cluster = _object(item, "cluster")
            _exact_fields(cluster, {"cluster_id", "idea_ids", "rationale"}, "cluster")
            cluster_id = _text(cluster.get("cluster_id"), "cluster.cluster_id")
            if cluster_id in cluster_ids:
                raise WorkbenchValidationError("clusters: duplicate cluster_id")
            cluster_ids.add(cluster_id)
            idea_ids = set(_texts(cluster.get("idea_ids"), "cluster.idea_ids"))
            if not idea_ids.issubset(ideas):
                raise WorkbenchValidationError("cluster.idea_ids: unknown idea")
            _text(cluster.get("rationale"), "cluster.rationale")
            count += 1
    if count == 0:
        raise WorkbenchValidationError("clusters: at least one clusters-v1.json required")
    return count


def _promoted(root: Path, byte_budget: int) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    promoted = root / "promoted"
    _safe_existing_dir(promoted, "ideation promoted")
    candidate_path = promoted / "candidate-input-v1.json"
    lineage_path = promoted / "lineage-v1.json"
    _safe_existing_file(candidate_path, "promoted candidate input")
    _safe_existing_file(lineage_path, "promoted lineage")
    return (
        _read_json(candidate_path, byte_budget, "promoted candidate input"),
        _read_json(lineage_path, byte_budget, "promoted lineage"),
    )


def _validate_lineage(
    lineage: Mapping[str, Any], *, candidate_input: Mapping[str, Any], candidate_ids: Iterable[str],
    ideas: Mapping[str, Mapping[str, Any]], relations: Mapping[str, Mapping[str, Any]],
    current_brief_hash: str,
) -> None:
    _exact_fields(lineage, {"candidate_input_hash", "records", "schema_version"}, "lineage")
    if lineage.get("schema_version") != LINEAGE_SCHEMA:
        raise WorkbenchValidationError("lineage.schema_version: idea-lineage-v1 required")
    if lineage.get("candidate_input_hash") != digest(candidate_input):
        raise WorkbenchValidationError("lineage.candidate_input_hash: must match promoted candidate input")
    records = lineage.get("records")
    if not isinstance(records, list):
        raise WorkbenchValidationError("lineage.records: array required")
    candidate_id_tuple = tuple(candidate_ids)
    expected = set(range(len(candidate_id_tuple)))
    seen = set()
    for record in records:
        item = _object(record, "lineage.records[]")
        _exact_fields(
            item,
            {"candidate_index", "rationale", "relation_ids", "source_idea_ids", "source_session_ids"},
            "lineage.records[]",
        )
        index = item.get("candidate_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise WorkbenchValidationError("lineage.candidate_index: zero-based integer required")
        if index in seen:
            raise WorkbenchValidationError("lineage.candidate_index: duplicate candidate index")
        seen.add(index)
        source_idea_ids = set(_texts(item.get("source_idea_ids"), f"lineage[{index}].source_idea_ids"))
        source_session_ids = _texts(item.get("source_session_ids"), f"lineage[{index}].source_session_ids")
        relation_ids = set(_texts(item.get("relation_ids", []), f"lineage[{index}].relation_ids", allow_empty=True))
        _text(item.get("rationale"), f"lineage[{index}].rationale")
        if not source_session_ids or not source_idea_ids.issubset(ideas):
            raise WorkbenchValidationError(f"lineage[{index}]: unknown source idea/session")
        actual_sessions = {str(ideas[idea_id].get("session_id")) for idea_id in source_idea_ids}
        if set(source_session_ids) != actual_sessions:
            raise WorkbenchValidationError(f"lineage[{index}].source_session_ids: must match source ideas")
        if not any(ideas[idea_id].get("brief_hash") == current_brief_hash for idea_id in source_idea_ids):
            raise WorkbenchValidationError(f"lineage[{index}]: at least one source idea must bind the current brief")
        if not relation_ids.issubset(relations):
            raise WorkbenchValidationError(f"lineage[{index}].relation_ids: unknown relation")
    if seen != expected:
        raise WorkbenchValidationError("lineage.records: exactly one record per promoted candidate index required")





def _validate_brief_bindings(
    brief: Mapping[str, Any], *, expected_run_id: str | None,
    expected_profile_revision_hash: str | None, expected_research_revision_hash: str | None,
    expected_evaluation_config_hash: str | None,
) -> None:
    if expected_run_id is not None and brief.get("run_id") != expected_run_id:
        raise WorkbenchValidationError("ideation brief.run_id: current run binding mismatch")
    if expected_profile_revision_hash is not None and brief.get("profile_revision_hash") != expected_profile_revision_hash:
        raise WorkbenchValidationError("ideation brief.profile_revision_hash: current profile binding mismatch")
    if expected_research_revision_hash is not None and brief.get("research_revision_hash") != expected_research_revision_hash:
        raise WorkbenchValidationError("ideation brief.research_revision_hash: current research binding mismatch")
    if expected_evaluation_config_hash is not None and brief.get("evaluation_config_hash") != expected_evaluation_config_hash:
        raise WorkbenchValidationError("ideation brief.evaluation_config_hash: current config binding mismatch")

def validate_workbench(
    root: Path,
    *,
    stage: str,
    byte_budget: int,
    connection=None,
    profile_connection=None,
    run_root: Path | None = None,
    run_id: str | None = None,
    config: EvaluationConfig | None = None,
    expected_run_id: str | None = None,
    expected_profile_revision_hash: str | None = None,
    expected_research_revision_hash: str | None = None,
    expected_evaluation_config_hash: str | None = None,
) -> dict[str, Any]:
    if stage not in {"diverge", "entangle", "promote"}:
        raise WorkbenchValidationError("ideation workbench stage must be diverge, entangle, or promote")
    _safe_existing_dir(root, "ideation workbench")
    brief, briefs = _briefs(root, byte_budget)
    current_brief_hash = digest(brief)
    _validate_brief_bindings(
        brief,
        expected_run_id=expected_run_id or run_id,
        expected_profile_revision_hash=expected_profile_revision_hash,
        expected_research_revision_hash=expected_research_revision_hash,
        expected_evaluation_config_hash=expected_evaluation_config_hash or (config.content_hash if config is not None else None),
    )
    if connection is not None and profile_connection is not None and run_id is not None and config is not None:
        profile = profile_payload(profile_connection)
        research = _current_artifact(connection, run_id, "research_bundle")
        _validate_brief_bindings(
            brief,
            expected_run_id=run_id,
            expected_profile_revision_hash=digest(profile),
            expected_research_revision_hash=research.content_hash,
            expected_evaluation_config_hash=config.content_hash,
        )
    ideas = _ideas(root, briefs, byte_budget)
    if not any(item.get("brief_hash") == current_brief_hash for item in ideas.values()):
        raise WorkbenchValidationError("ideas: at least one idea must bind the current brief")
    relations: dict[str, Mapping[str, Any]] = {}
    cluster_count = 0
    candidate_ids: tuple[str, ...] = ()
    candidate_input: Mapping[str, Any] | None = None
    if stage in {"entangle", "promote"}:
        relations = _relations(root, ideas, byte_budget, briefs)
        cluster_count = _clusters(root, ideas, byte_budget, briefs)
    if stage == "promote":
        if connection is None or profile_connection is None or run_root is None or run_id is None or config is None:
            raise WorkbenchValidationError("promote validation requires current run/profile bindings")
        candidate_input, lineage = _promoted(root, byte_budget)
        profile = profile_payload(profile_connection)
        research = _current_artifact(connection, run_id, "research_bundle")
        _reject_todos(candidate_input, "promoted candidate input")
        _reject_todos(lineage, "promoted lineage")
        validated = validate_candidate_input(
            profile=profile, research=research, candidate_input=candidate_input, config=config,
        )
        if len(validated.candidates) > 12:
            raise WorkbenchValidationError("promoted candidate input: at most 12 candidates allowed")
        candidate_ids = tuple(candidate.candidate_id for candidate in validated.candidates)
        _validate_lineage(
            lineage, candidate_input=candidate_input, candidate_ids=candidate_ids,
            ideas=ideas, relations=relations, current_brief_hash=current_brief_hash,
        )
    return {
        "brief_id": brief["brief_id"],
        "brief_hash": current_brief_hash,
        "candidate_input_hash": digest(candidate_input) if candidate_input is not None else None,
        "candidate_ids_by_index": {str(index): candidate_id for index, candidate_id in enumerate(candidate_ids)},
        "cluster_count": cluster_count,
        "counts": {
            "candidates": len(candidate_ids),
            "clusters": cluster_count,
            "ideas": len(ideas),
            "relations": len(relations),
        },
        "idea_count": len(ideas),
        "relation_count": len(relations),
        "schema_version": WORKBENCH_SCHEMA,
        "stage": stage,
        "status": "workbench_valid",
        "advisories": workbench_advisories(ideas.values(), relations.values(), candidate_input),
    }


__all__ = [
    "BRIEF_SCHEMA", "CLUSTER_SCHEMA", "IDEA_SCHEMA", "LINEAGE_SCHEMA", "RELATION_SCHEMA",
    "WORKBENCH_SCHEMA", "WorkbenchValidationError", "initialize_workbench",
    "scaffold_ideation_brief", "validate_workbench",
]
