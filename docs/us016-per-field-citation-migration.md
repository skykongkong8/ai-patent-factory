# US-016 migration — per-field citation binding on `CandidateClaim`

Status: applied. Schema version is unchanged (`candidate-v1`); the *shape* of
`claims[]` changed, so this is a breaking input change without a version bump.

## What changed

`candidate-input-v1` candidates carry `claims[]`, previously
`{claim, field}`. Each entry now additionally requires `evidence_references`:

```json
{"claim": {"label": "hypothesis"}, "evidence_references": [], "field": "expected_effects"}
```

`evidence_references` uses the same entry shape as the candidate-level
`evidence_references` (`content_hash`, `evidence_id`, `limitation`, `span_hash`).

Two rules apply:

- The list is **required but may be empty**. A field with no evidentiary
  support says so explicitly instead of inheriting the candidate-level blob.
- Every `evidence_id` listed must also appear in the candidate-level
  `evidence_references`, so `report._cited_ids` and the appendix stay complete.

## Why it extends `CandidateClaim` rather than adding a sibling map

`CandidateClaim` was already the field-keyed per-field structure: it carries
`field` plus the epistemic `Claim` labelling that field. A separate
`field -> evidence_references` map would duplicate the same key space and could
drift out of sync with the labels — a field could assert `creative_suggestion`
in `claims` while carrying prior-art citations in the sibling map, which is the
citation-hygiene defect this work exists to close. Binding the references onto
the claim makes the label and its support one atomic unit and reuses the
existing claim/evidence cross-check in `Candidate.from_dict`.

Note that hedging is a property of the **render site**, not of the claim label:
`technical_problem` renders hedged in report section 3 and unhedged in section
6. So `report.HEDGED_LINE_KEYS` — not the claim label — decides whether a bullet
prints a citation. The model deliberately does **not** forbid references on a
`hypothesis` or `creative_suggestion` claim.

## Replaying a pre-change candidate input

`ideation._exact_fields` rejects unknown **and** missing fields, so a
pre-change input fails loudly rather than silently:

```
candidate_input.candidates[0].claims[0]: missing fields: evidence_references
```

Mechanical fix — add the key to every entry in every `claims[]`:

```bash
python3 - "$INPUT" <<'PY'
import json, sys
path = sys.argv[1]
data = json.loads(open(path, encoding="utf-8").read())
for candidate in data["candidates"]:
    for entry in candidate["claims"]:
        entry.setdefault("evidence_references", [])
open(path, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2))
PY
```

Adding empty lists reproduces the *hygiene* fix (hedged bullets stop citing) but
not the *binding* (unhedged bullets cite nothing until you fill
`technical_problem` / `mechanism`). `python3 -m patent_factory scaffold candidate`
now pre-fills both of those, so regenerating the scaffold is preferable to
patching by hand.

**This changes `candidate_id`.** `candidate_id` is `ca_ + digest(body)[:20]` over
a body that includes `claims`, so every replayed candidate gets a new id, and
that cascades to `candidate_set` -> `finalist_set` -> `corpus_set` ->
`feature_map_set` -> `audit_batch` -> report bindings. A partially completed run
cannot be resumed across this change; restart it from `/ideate`.

## Rollback

There are **no committed example candidate JSONs to snapshot** — the brief
assumed some existed under `examples/`, but the only committed candidate-shaped
file in the repo is `schemas/candidate.schema.json` itself (verified with
`git ls-files '*.json' | xargs grep -l 'candidate-input-v1\|"technical_problem"'`).
`examples/` holds only `justin/{README.md,background.md,expected-report-en.md,web-rows.json}`
and `redacted/interview.json`. Git history is therefore the whole snapshot.

To roll back, revert the commit carrying this note. The reverting change must
touch all of:

- `src/patent_factory/ideation.py` — `CandidateClaim` fields and the subset guard
- `src/patent_factory/report.py` — `HEDGED_LABELS`, `HEDGED_LINE_KEYS`,
  `_field_reference_tokens`, and the `bullet()` call sites in `_section_bodies`
- `src/patent_factory/validation.py` — `_hedged_citation_check`
- `src/patent_factory/scaffold.py` — the pre-filled `evidence_references`
- `schemas/candidate.schema.json` — the `candidateClaim` `$def`

Rolling back re-introduces prior-art citations on hedged bullets and re-changes
every `candidate_id` again.

## Golden report

`examples/justin/expected-report-en.md` still contains 18 hedged lines carrying
prior-art citations — the exact defect. It is **deliberately not regenerated
here**: `tests/e2e/test_full_journey.py` is expected-red for this batch and the
coordinator regenerates the golden once, in US-017.
