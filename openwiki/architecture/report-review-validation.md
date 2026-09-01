---
type: architecture concept
title: Report Rendering, Review, and Release Validation
description: The versioned bilingual report pipeline renders a hash-bound private report, subjects it to an independent review, and deterministically validates the result before completion. It also defines the sensitive-disclosure gate that protects external sharing and the revision rules that preserve provenance.
tags: [reports, review, validation, provenance, privacy, release-gates]
verified:
  - by: openwiki/0.4.3
    at: 2026-09-01T07:14:54.600Z
---

# Report Rendering, Review, and Release Validation

This is the release boundary for the report workflow. A drafter supplies structured input; the renderer combines it with the current approved profile, research, candidates, finalists, corpus, feature map, scorer configuration, and KIPRIS audit. A separate reviewer then records an independent decision, and a deterministic validator reconstructs the report before the run may become `complete`.

The report is bilingual by policy: `report-en-v1.0.0.json` and `templates/report-en.md` define the English contract, while the corresponding Korean files remain supported. The input schema `report-input-v2` carries `language` (`en` or `ko`); legacy `report-input-v1` is normalized to Korean. Narrative is rendered in the selected language, but source titles and identifiers retain their original language.

## Lifecycle and control flow

```mermaid
flowchart TD
    A[Current audit approved] --> B[Draft private report]
    B --> C{Sensitive fields in report]
    C -->|No| D[Independent review]
    C -->|Yes| D
    D --> E{Review disposition]
    E -->|revise| F[Revision required]
    F --> G[New hash-bound draft]
    G --> D
    E -->|approved| H[Deterministic validation]
    H --> I{All checks pass]
    I -->|No| F
    I -->|Yes| J[Validated and complete]
    J --> K{External share requested]
    K -->|No| L[Private completion]
    K -->|Yes| M[Sensitive-disclosure gate]
    M -->|redact| G
    M -->|stop| N[Stopped]
    M -->|approve exact scope| O[External export and receipt]
```

This flow shows the two distinct revision-required paths: an independent review can require corrections, and validation can reject a report that passed review. Sensitive disclosure is not required merely to create a private draft or complete a private run; it is required before an owner-approved report is exported outside its owner-only run.

## Versioned rendering contract

`publish_report` is the report entrypoint. It first rejects credential canaries, validates the exact input shape, checks that the run is in `audit_approved`, `revision_required`, or `draft_ready`, and then reads only current, non-stale artifacts. An initial draft from `audit_approved` cannot claim a revision. A revision must bind both the current report hash and current review hash, and must be justified by a blocking review or failed validation.

The renderer requires at least three current finalists and an authoritative current audit. The audit must bind the finalist set, retained corpus, feature-map set, and scorer configuration; it is revalidated with the frozen `SimilarityConfig`. There must be exactly one audit result for every current finalist/candidate pair: empty, omitted, duplicate, extra, or mismatched results stop report creation. Evidence references are resolved through the research bundle, corpus projection, and authoritative `evidence_records` table.

The eleven policy-owned sections are fixed and ordered:

1. Document Purpose, Scope, and Disclaimer
2. Inventor Technical Background and Domain Context
3. Problem and Opportunity Areas
4. Research Scope and Method
5. Key Prior-Art Landscape
6. Final Candidates
7. Candidate Comparison Matrix
8. Final KIPRIS Similarity-Risk Audit
9. User Decisions at Similarity Checkpoints
10. Patent-Attorney Handoff Questions and Follow-Up Investigations
11. Source and Evidence Appendix

`render_report_markdown` fills each template placeholder exactly once, rejects unresolved placeholders, and emits one H1 plus the eleven numbered H2 headings. `validate_report_artifact` repeats the contract checks, verifies the language policy and policy hash, binds the template hash, requires the exact section bodies and headings, and compares stored Markdown with a fresh rendering. The structured `draft_spec` (handoff questions, selected profile fields, and recommended investigations) is itself hash-bound, so it cannot be silently replaced by free-form text.

The legal disclaimer is mandatory and must remain verbatim in section 1:

> The output of this tool is invention-organizing support material and is not legal advice. It provides no legal conclusion on patentability, novelty, validity, or non-infringement/FTO; confirm any decision that matters with a qualified patent attorney.

The similarity disclaimer is mandatory in section 8:

> All similarity figures are provisional research-aid indicators within the retrieved corpus only, and are not a legal determination of novelty, inventive step, patentability, or non-infringement/FTO.

Citation tokens have the form `[@ev_<16 lowercase hex>]`. Every token must resolve to current evidence metadata, and the appendix must contain the same identifiers exactly once in sorted order. Citation metadata includes content hash, identifier, title, limitations, observation date, source type, and URL. A safe source URL, when present, is HTTPS without credentials, fragments, or an unsafe port. Hedged lines such as `[candidate hypothesis]` or `[creative suggestion]` must not also carry a prior-art citation: the validator checks this at line level, not merely by comparing document-wide token sets.

The renderer deliberately distinguishes sourced assertions from suggestions. Candidate components, effects, fit, implementation examples, problems, and unresolved questions can be marked as hypotheses or creative suggestions. Axis figures are author-supplied and are displayed neither as computed scores nor as the basis for candidate ordering; shortlist priority remains the recorded ordering basis.

## Independent review boundary

`run_review` reads and validates the current report, runs the deterministic legal-language scan before trusting any reviewer checkbox, and requires the review `report_hash` to equal the current report hash. The reviewer identity and `pass_id` must differ from the drafter's identity and pass. This is a separation of passes, not just a different label in one payload.

The review input must contain the seven policy-owned checks, in order: `citation_integrity`, `decision_gate_coverage`, `factual_grounding`, `internal_consistency`, `legal_language`, `schema_completeness`, and `source_coverage`. It also carries findings, evidence corrections, prohibited-language findings, and a decision-gate verification. The covered finalist IDs must exactly match the current audit's `decision_required` results and the verification must bind the report's `audit_batch` hash.

Disposition is derived, not caller-selected: any failed check, blocking finding, evidence correction, prohibited-language finding, or failed decision-gate verification produces `revise`; otherwise it produces `approved`. An advisory finding alone is allowed. An approved review moves the run to `reviewed`; a revision disposition moves it to `revision_required`.

Review artifacts preserve the report policy hash, complete report bindings, report hash, reviewer identity, and schema version `review-v1`. The reviewer cannot edit the report artifact. Any new report revision invalidates dependent review and validation artifacts through the artifact dependency graph.

## Deterministic validation and completion

`validate_and_complete` operates only from `reviewed` or `validated` (with an idempotent replay for an already `complete` run). It builds nine checks in fixed order:

- `artifact_bindings`
- `citation_integrity`
- `decision_coverage`
- `identifier_shape`
- `legal_language`
- `narrative_language`
- `report_structure`
- `review_binding`
- `semantic_reconstruction`

The manifest is `validation-v1`, uses validator version `report-validator-v1.0.0`, records all relevant artifact hashes and schema versions, and has status `passed` only when every check passes. Semantic reconstruction calls the same report payload builder using the stored structured draft specification and redaction history; any extra, omitted, reordered, or altered report material fails even if its Markdown and citations otherwise look valid. Decision coverage also checks that checkpoint action, reason, feedback, and retain warnings appear in section 9.

A failed validation is persisted and sends the run to `revision_required`; it is not a successful release. StateStore's completion kernel performs a second guard: current report, review, and validation artifacts must exist with supported schemas; their content hashes must match content plus dependency edges; report and review must bind; validation must be passed and current; and the required report-to-review, report-to-validation, and review-to-validation edges must exist. It recomputes the validation manifest before allowing `complete`. Generic or caller-authored transitions cannot bypass these invariants.

## Sensitive disclosure and sharing

Private report generation and normal private completion do not disclose content. `share_report` is the external boundary. It accepts only `external-report-share-v1`, requires a destination outside the owner-only run, and validates the current report, review, and validation artifacts plus the requested report hash. The requested sensitive fields must exactly equal the report's sorted sensitive-disclosure fields.

The share scope binds the report hash, Markdown content hash, review hash, validation hash, recipient, destination, purpose, and each field's reason and text hash. Without a matching decision, the operation creates or reuses a `sensitive_disclosure` gate and raises `SensitiveDisclosureRequiredError`. Approval must match that exact scope, subject report revision, and suspended operation; it is consumed once. The managed destination subtree is `.patent-factory-shares`, owner-only (`0700`), non-symbolic, and the share exports the Markdown alongside an immutable `external-share-receipt-v1`.

Redaction is a new report revision, not an in-place edit. `apply_sensitive_redaction` requires the exact consumed redact decision, replaces sensitive text with the language-specific marker, records a non-plaintext history containing the prior report hash, field, reason, decision ID, and text hash, and invalidates review and validation so the redacted report must be reviewed again. Stop is terminal. An exact retry of a completed share replays its immutable receipt/export without consuming another approval; changing recipient, destination, purpose, report, or sensitive scope requires a new gate.

## Operational entrypoints

The CLI exposes the same boundaries as the library:

```text
python3 -m patent_factory scaffold report      --language en --out workspace/requests/report-input-v2.json
python3 -m patent_factory draft   --run RUN --run-id ID --input workspace/requests/report-input-v2.json
python3 -m patent_factory review  --run RUN --run-id ID --input workspace/requests/review-input-v1.json
python3 -m patent_factory validate --run RUN --run-id ID
python3 -m patent_factory share   --run RUN --run-id ID --input workspace/requests/share-input-v1.json
```

Use `run status` to inspect the state and hashes of current artifacts and `run show` to inspect one artifact. Keep draft inputs and report exports under the private workspace; do not hand-copy hashes—use the scaffold commands to create pre-bound request files. Network retrieval is upstream of this page; report rendering itself makes no network or model calls.

## Focused tests and safe extension points

`tests/integration/test_g007_report_review_validation.py` is the principal contract suite. It covers CLI draft-review-validate completion, exact schema validation, disclaimer and appendix rules, reviewer/drafter separation, sentence-local legal scanning, advisory findings, forged validation rejection, content-hash tampering, authoritative audit result cardinality, frozen policy drift, canonical reconstruction of claims/matrix/appendix, sensitive disclosure and redaction, and exact share replay. `test_g009_english_report.py` exercises the English policy/template and narrative output. `test_us016_citation_binding.py` protects per-field citation binding and the no-citation-on-hedged-line invariant.

Safe extensions should add a new policy/template version rather than changing frozen arrays or literal disclaimers in place; update the report, review, and validation schemas together. New report material must be produced by the canonical payload builder and covered by semantic reconstruction, with bindings and dependency edges added deliberately. New review checks require synchronized policy, review schema, validator ordering, and tests. New disclosure fields must be represented in the exact share scope and redaction history, never merely filtered during export.
