# `workspace/` — generated state, exports & requests

This folder holds everything the core produces, plus the **versioned request files
you author** to drive each stage. Everything under it **except this README is
git-ignored**.

## What lives here

```
workspace/
  profile.sqlite3   # AUTHORITATIVE profile state — never hand-edit
  profile.json      # deterministic export regenerated from the DB — never hand-edit
  requests/         # the *-input-v1.json you author for each stage (see below)
  runs/<RUN>/
    factory.sqlite3     # authoritative per-run state
    research-exports/   # immutable research bundle manifests
    …                   # report / review / share exports
```

`profile.sqlite3` is the single source of truth; `profile.json` is a byte-for-byte
export of the committed DB. Do not edit either by hand, and do not bypass
`conflict_resolution_required` — resolve conflicts through `profile conflict-inspect`
/ `profile conflict-decide`.

## Versioned request files (`workspace/requests/`)

Each stage takes a versioned `*-input-v1.json`. **You** author these (with Claude's
help via the slash commands); the core validates them against `schemas/*.json`, binds
them to prior-stage hashes, and records state. The templates below are faithful
skeletons — trim/extend per the schema.

> **ID and hash fields are not invented.** Values like `ca_…` (candidate),
> `fi_…` (finalist), `ev_…` (evidence), `cl_…` (claim), every `*_hash`,
> `report_hash`, `audit_hash`, `gate_id`, and `subject_revision_hash` are **copied
> from a previous command's JSON output**. If you don't have one yet, run the earlier
> stage first — the core rejects fabricated bindings.

### `candidate-input-v1.json` → `/ideate`

The richest input: ≥3 candidates, each tracing to profile facts and research
evidence. `claims` needs ≥4 items, `profile_references` ≥2, `evidence_references` ≥1.
(The core injects `candidate_id`, `profile_revision_hash`, `research_revision_hash`,
and `evaluation_config_hash` — you don't supply them.) Full field list:
`schemas/candidate.schema.json`.

```json
{
  "schema_version": "candidate-input-v1",
  "candidates": [
    {
      "title": "센서 보정 후보 1",
      "domain": "센서",
      "technical_problem": "센서 오차",
      "mechanism": "보정 메커니즘",
      "implementation_example": "센서 출력에 보정기를 연결한다",
      "measurable_validation": "평균 절대 오차를 비교한다",
      "claims": [{}, {}, {}, {}],
      "components": ["센서", "보정기"],
      "interactions": ["보정기가 센서 출력을 조정한다"],
      "transformations": ["오차 보정"],
      "required_inputs": ["센서 출력"],
      "outputs": ["보정 출력"],
      "expected_effects": ["오차 감소"],
      "profile_references": [
        {"claim_id": "cl_0000000000000000", "field": "project_summary", "kind": "problem"},
        {"claim_id": "cl_1111111111111111", "field": "expertise", "kind": "capability"}
      ],
      "evidence_references": [
        {"evidence_id": "ev_...", "content_hash": "...", "span_hash": "...", "limitation": null}
      ],
      "synthesis_trace": {
        "method": "adapt",
        "evidence_ids": ["ev_..."],
        "narrative": "공개 메커니즘을 사용자 제약에 맞게 조정한 휴리스틱 기여"
      },
      "unresolved_dependencies": [],
      "unresolved_questions": ["현장 잡음 분포"]
    }
  ]
}
```

### `shortlist-input-v1.json` → `/shortlist`

Three finalists, each scored on the three fixed axes (`differentiation`,
`technical_feasibility`, `utility_significance`). Schema: `schemas/finalist.schema.json`.

```json
{
  "schema_version": "shortlist-input-v1",
  "finalists": [
    {
      "candidate_id": "ca_00000000000000000000",
      "priority": 1,
      "selection_rationale": "세 축의 구조적 근거가 완전하다",
      "axes": [
        {
          "axis": "differentiation",
          "score": 60,
          "confidence": "medium",
          "rationale": "differentiation 구조적 근거",
          "rubric_version": "<from config/evaluation rubric>",
          "supporting_evidence_references": [{"evidence_id": "ev_...", "content_hash": "...", "span_hash": "...", "limitation": null}],
          "contrary_evidence_references": [],
          "coverage_assessment": "현재 검색된 공개 자료 범위에서만 평가",
          "coverage_limitations": ["최종 KIPRIS 재검색 전의 예비 평가"],
          "gaps": []
        }
      ]
    }
  ],
  "exclusions": [
    {"candidate_id": "ca_...", "rationale": "현재 선택 우선순위 밖", "reason_codes": ["not_selected"]}
  ],
  "insufficiency": null
}
```

Each finalist's `axes` must include all three axis names. If three defensible
finalists are not available, set `finalists: []` and describe `insufficiency` instead
of inventing weak ones.

### `audit-query-input-v1.json` → `/audit retrieve`  ·  fixture manifest under `documents/`

```json
{
  "schema_version": "audit-query-input-v1",
  "finalist_set_hash": "<from the shortlist output>",
  "groups": [
    {"finalist_id": "fi_...", "queries": [
      {"language": "ko", "term": "센서 보정"},
      {"language": "en", "term": "sensor calibration"}
    ]}
  ]
}
```

The fixture manifest is read from the **documents root**
(`documents/requests/audit-fixture-manifest-v1.json`):

```json
{
  "schema_version": "audit-fixture-manifest-v1",
  "responses": [
    {"finalist_id": "fi_...", "term": "센서 보정", "page": 1, "source": "documents/happy.xml"}
  ]
}
```

### `feature-map-set-input-v1.json` → `/audit score`

Reviewed feature maps per finalist; the scorer is fixed at `simrisk-v1.0.0`.

> **`schemas/feature-map.schema.json` describes the persisted *artifact***
> (`feature-map-set-v1`, what `/audit score` *writes*), **not this input.**
> Writing this input against that schema produces a shape the verb rejects. The
> authoritative input contract is `similarity.validate_feature_map` (per-map
> fields, category weights, decision shape) plus the cross-field checks in
> `audit.run_audit_scoring` (`src/patent_factory/audit.py:390-472`: hash
> bindings, exactly one map per current finalist, span provenance, one reviewed
> decision per retained corpus record).

Each entry in `maps` wraps a `feature_map` with the finalist it belongs to and a
`map_id`. **Do not hand-author `map_id`**: it digests the *filled* map, so it
changes the moment you edit a `status` or `rationale`, and a stale one is
rejected ("map identity does not bind frozen content"). Fill in every judgment
field first, then derive `map_id` last with:

```bash
python3 -m patent_factory scaffold feature-map \
  --seal workspace/requests/feature-map-set-input-v1.json \
  --out workspace/requests/feature-map-set-input-v1.json
```

(`scaffold.seal_feature_map_input` re-derives every `map_id` from
`audit.feature_map_id`; it refuses to seal a map that still has an unfilled
`TODO(agent)` marker.)

The example below is validated byte-for-byte by
`tests/unit/test_documented_contracts.py`, which loads it straight out of this
file and asserts `similarity.canonical_feature_map` /
`similarity.validate_feature_map` accept every `feature_map`. A full submission
needs one `maps` entry per current finalist (3 for a full shortlist); only one
is shown here for brevity.

<!-- feature-map-example:start -->
```json
{
  "schema_version": "feature-map-set-input-v1",
  "finalist_set_hash": "<from the shortlist output>",
  "corpus_set_hash": "<from the audit retrieve output>",
  "maps": [
    {
      "finalist_id": "<finalist_id from the shortlist output>",
      "feature_map": {
        "candidate_classifications": ["G06F 1/00"],
        "features": [
          {"feature_id": "feature-problem", "category": "problem", "essential": true, "weight": "0.10",
           "candidate_span_hashes": ["<span hash cited from the finalist/candidate text>"]},
          {"feature_id": "feature-inputs", "category": "inputs", "essential": true, "weight": "0.10",
           "candidate_span_hashes": ["<span hash>"]},
          {"feature_id": "feature-mechanism", "category": "mechanism", "essential": true, "weight": "0.30",
           "candidate_span_hashes": ["<span hash>"]},
          {"feature_id": "feature-transformations", "category": "transformations", "essential": true, "weight": "0.20",
           "candidate_span_hashes": ["<span hash>"]},
          {"feature_id": "feature-outputs", "category": "outputs", "essential": true, "weight": "0.10",
           "candidate_span_hashes": ["<span hash>"]},
          {"feature_id": "feature-technical_effects", "category": "technical_effects", "essential": true, "weight": "0.20",
           "candidate_span_hashes": ["<span hash>"]}
        ],
        "reference_maps": [
          {
            "evidence_id": "ev_0000000000000000",
            "inspected_fields": ["title", "abstract", "classifications"],
            "decisions": [
              {"feature_id": "feature-problem", "status": "matched", "rationale": "reviewer's stated basis, tied to the inspected fields", "reference_span_hashes": ["<span hash from the reviewed reference record>"]},
              {"feature_id": "feature-inputs", "status": "matched", "rationale": "reviewer's stated basis", "reference_span_hashes": ["<span hash>"]},
              {"feature_id": "feature-mechanism", "status": "different", "rationale": "reviewer's stated basis", "reference_span_hashes": ["<span hash>"]},
              {"feature_id": "feature-transformations", "status": "not_disclosed", "rationale": "reviewer's stated basis", "reference_span_hashes": ["<span hash>"]},
              {"feature_id": "feature-outputs", "status": "unavailable", "rationale": "reviewer's stated basis", "reference_span_hashes": []},
              {"feature_id": "feature-technical_effects", "status": "matched", "rationale": "reviewer's stated basis", "reference_span_hashes": ["<span hash>"]}
            ]
          }
        ],
        "review": {
          "reviewed_at": "2026-07-19T00:00:00Z",
          "reviewed_by": "<reviewer id>",
          "status": "reviewed"
        }
      },
      "map_id": "<derived last by 'scaffold feature-map --seal' — never hand-authored>"
    }
  ]
}
```
<!-- feature-map-example:end -->

`features` weights must total exactly the `simrisk-v1.0.0` category weights in
`config/simrisk-v1.0.0.json` (`problem` 0.10, `inputs` 0.10, `mechanism` 0.30,
`transformations` 0.20, `outputs` 0.10, `technical_effects` 0.20 — the split
above). Each `reference_maps` entry needs one `decisions` entry per feature
(`status` one of `matched`/`different`/`not_disclosed`/`unavailable`); every
status except `unavailable` needs a non-empty `reference_span_hashes`.

### `report-input-v2.json` → `/draft`

The report renders in English (`"language": "en"`, the default recommendation) or
Korean (`"language": "ko"`). The older `report-input-v1` shape (no `language`
field) is still accepted and means Korean.

```json
{
  "schema_version": "report-input-v2",
  "language": "en",
  "drafter": {"id": "drafter", "pass_id": "draft-pass", "type": "agent"},
  "report_date": "2026-07-14",
  "profile_fields": ["expertise", "project_summary", "technical_domain"],
  "handoff_questions": ["Can the differentiating features be claimed independently?"],
  "recommended_investigations": ["Confirm additional embodiments"],
  "sensitive_disclosures": [],
  "revision": null
}
```

### `review-input-v1.json` → `/review`

The reviewer's `id`/`pass_id` must differ from the drafter's. `report_hash` comes
from the `/draft` output; all seven checks must be present.

```json
{
  "schema_version": "review-input-v1",
  "reviewer": {"id": "reviewer", "pass_id": "review-pass", "type": "agent"},
  "report_hash": "<from the draft output>",
  "disposition": "approved",
  "checks": [
    {"name": "citation_integrity", "status": "pass", "details": "독립 검토 통과"},
    {"name": "decision_gate_coverage", "status": "pass", "details": "..."},
    {"name": "factual_grounding", "status": "pass", "details": "..."},
    {"name": "internal_consistency", "status": "pass", "details": "..."},
    {"name": "legal_language", "status": "pass", "details": "..."},
    {"name": "schema_completeness", "status": "pass", "details": "..."},
    {"name": "source_coverage", "status": "pass", "details": "..."}
  ],
  "decision_gate_verification": {"audit_hash": "<from audit>", "covered_finalist_ids": [], "status": "pass"},
  "evidence_corrections": [],
  "findings": [],
  "prohibited_language_findings": []
}
```

### `gate-decision-input-v1.json` → `gate decide` (and to resume `share`)

Author this only when a gate stops the pipeline. Copy `gate_id`,
`subject_revision_hash`, and `approval_scope` verbatim from the stop; you supply
`action`, `reason`, and `actor`.

```json
{
  "schema_version": "gate-decision-input-v1",
  "gate_id": "<from the stop>",
  "subject_revision_hash": "<from the stop>",
  "approval_scope": "<from the gate>",
  "action": "approve",
  "actor": "user",
  "reason": "reviewed current evidence",
  "decisions": [],
  "plan": {}
}
```

> **`schemas/decision.schema.json` describes the persisted *output***
> (`decision-set-v1`), **not what you author.** It lists all seven
> `decisions[]` fields (`action`, `candidate_id`, `corpus_hash`,
> `feature_map_id`, `finalist_hash`, `finalist_id`, `reason`, `warning`)
> because that is what the core *writes* to the artifact. For an
> `excessive_similarity` gate, **you only ever supply**
> `{"action", "finalist_id", "reason"}` per affected finalist
> (`src/patent_factory/decisions.py:226-227`); the core looks up and derives
> `candidate_id`, `corpus_hash`, `feature_map_id`, and `finalist_hash` itself
> from the current finalist/corpus/feature-map sets, and fills `warning`
> (`decisions.py:243-249`). Example `decisions` entry for that gate kind:
>
> ```json
> {"action": "retain_with_warning", "finalist_id": "<affected finalist_id>", "reason": "reviewed provisional similarity risk and accept it"}
> ```
>
> This `gate-decision-input-v1` shape is for the gate kinds that predate the
> checkpoint (`credential`, `coverage`, `domain_pivot`, `sensitive_disclosure`,
> and any `excessive_similarity` gate raised before this feature — kept valid
> for replay). A **`post_audit_checkpoint`** gate rejects v1 outright and
> requires `gate-decision-input-v2` instead — see below.

### `gate-decision-input-v2.json` → `gate decide` (post-audit checkpoint only)

Every `audit score` now raises a `post_audit_checkpoint` gate — clean or
breaching. Start from `scaffold gate-decision` (every hash/ID binding pre-filled
from the pending gate; every judgment field left `TODO(agent)`):

```bash
python3 -m patent_factory scaffold gate-decision --run RUN --run-id RUN_ID \
  --gate-id GATE_ID --out workspace/requests/gate-decision-input-v2.json
```

You author `action` (`approve` / `re_ideate` / `re_research` / `stop`), `actor`,
`reason`, per-finalist `feedback` (`{finalist_id, interesting, boring}` for **all
three** current finalists, on every action), and — only for `re_research` — a
bounded `plan`. `gate decide`'s core validation rejects any decision that still
has a `TODO(agent)` marker anywhere in it.

```json
{
  "schema_version": "gate-decision-input-v2",
  "gate_id": "<from the stop>",
  "subject_revision_hash": "<from the stop>",
  "approval_scope": "<from the gate, verbatim>",
  "action": "approve",
  "actor": "user",
  "reason": "reviewed the dossier; approving for draft",
  "decisions": [],
  "feedback": [
    {"finalist_id": "<finalist_id from the gate>", "interesting": "worth pursuing further", "boring": "felt like a narrower variant"}
  ],
  "plan": {}
}
```

`decisions` follows the same "only what you decide, the core derives the rest"
rule as v1, with one extra composition rule: it is **only ever non-empty on
`approve`**, and only when the gate's `approval_scope.affected_finalist_ids` is
non-empty (a breaching audit) — one `{"action": "retain_with_warning",
"finalist_id": "<affected finalist_id>", "reason": "..."}` entry per breaching
finalist, exactly like the legacy `excessive_similarity` shape above. Every other
action (`re_ideate` / `re_research` / `stop`), and `approve` on a clean audit,
keeps `decisions: []` — the per-finalist signal for those cases lives only in
`feedback`.

On `re_research`, `plan` must be a non-empty bounded object (what additional
research is needed — offline query terms / needed-research notes); every other
action must leave it `{}`:

```json
{"plan": {"needed_research": ["broader prior-art sweep for the sensor mechanism"]}}
```

`re_research` re-enters research for exactly one **offline** second pass
(`research fixture` / `research normalize-web` + `research manual`); live
`research kipris` / `research serpapi` on this pass is deferred to
[issue #48](https://github.com/skykongkong8/ai-patent-factory/issues/48).

### `external-report-share-v1.json` → `/review` (share)

```json
{
  "schema_version": "external-report-share-v1",
  "report_hash": "<hash of the validated report>",
  "recipient": "attorney@example.test",
  "purpose": "변리사 검토",
  "destination": "shares",
  "sensitive_fields": ["candidate.1.mechanism"]
}
```

`share` stops at `sensitive_disclosure_required`; resume it by adding the core-issued
`--decision-id` after you resolve the gate. Copying a file is not sharing.
