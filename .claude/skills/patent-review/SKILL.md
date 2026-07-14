---
name: patent-review
description: Perform an independent, hash-bound Korean report review and deterministic validation without making legal conclusions.
---

# Patent review workflow

The reviewer identity and pass must differ from the drafter. Review the exact report hash and submit only `review-input-v1` through `python3 -m patent_factory review`. Check all eleven Korean sections, evidence/citation resolution, feature comparisons, coverage limitations, current human decisions, report bindings, and prohibited legal overclaims.

Similarity is a research aid within the retrieved corpus. Reject unqualified patentability, novelty/inventive-step, validity, non-infringement/FTO, or legal-advice statements. Do not fix the report export in place. Findings produce `revision_required`; only a corrected new report revision may receive a new review.

Run `validate` only after stdout reports `reviewed`, and accept only `complete`. External sharing is a separate `external-report-share-v1` operation and may stop at `sensitive_disclosure_required`. This skill cannot create an approval or egress authorization; require the user's current exact `gate-decision-input-v1` and the core-issued decision ID.
