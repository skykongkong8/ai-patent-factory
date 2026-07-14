# G006 gate decision contract

Every decision is bound to one pending gate, its current subject revision hash, exact
approval scope, and recorded suspended operation. Callers choose an action but never a
resume state. The state kernel owns all branch targets and publishes a private immutable
`decision-set-v1` artifact in the same transaction as the decision row, event, one-time
claim, state update, export registry, and idempotency record.

Coverage supports `expand`, `retry`, and `stop`. Expansion enters `research_running`;
retry enters `audit_running`; neither can approve an incomplete audit. The stored plan is
intent only: G006 does not fabricate research or audit artifacts.

An excessive audit requires exactly one current choice for every affected finalist.
Missing, duplicate, extra, or stale bindings are rejected. All retain choices enter
`audit_approved` and preserve an explicit warning. Any refine choice enters
`ideation_running`; any replace/research choice takes precedence and enters
`research_running`; stop is terminal. Old finalists and audits remain immutable history,
while normal dependency invalidation makes obsolete decision artifacts stale.

Conflict, credential, sensitive-disclosure, and domain-pivot actions use the same exact
gate infrastructure. Approval actions remain consumable once by only the recorded
operation. Conflict profile mutation still uses the pre-existing profile database path;
this contract does not pretend it has been migrated into the run ledger. Sensitive
disclosure is infrastructure only in G006: report rendering, redaction, sharing, review,
and validation remain G007 work.
