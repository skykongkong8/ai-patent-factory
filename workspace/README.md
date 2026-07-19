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

Reviewed feature maps per finalist; the scorer is fixed at `simrisk-v1.0.0`. Minimal
shape (full structure: `schemas/feature-map.schema.json`):

```json
{
  "schema_version": "feature-map-set-input-v1",
  "finalist_set_hash": "<from shortlist>",
  "corpus_set_hash": "<from audit retrieve>",
  "maps": [
    {"feature_map": {"reference_maps": [{"decisions": {"feature-problem": {"status": "matched"}}}]}}
  ]
}
```

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
