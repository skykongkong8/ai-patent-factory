# G007 Korean report contract

The portable core renders one private, immutable Korean Markdown report from exact current
approved artifacts. The renderer performs no network or model calls. It emits one H1 and
the eleven policy-owned H2 headings in `templates/report-ko.md`.

Evidence citations use only `[@ev_<16 lowercase hex>]`. Every cited identifier resolves
to an exact current research or final-audit corpus record, and the appendix contains the
same identifier set exactly once in sorted order. Source titles and identifiers remain in
their original language. Generated narrative is Korean-first.

The drafter and reviewer are separate structured-input passes. A review is bound to one
report hash and cannot edit it. A report revision invalidates its prior review and
validation through the artifact dependency graph. Only a current approved review and a
deterministic passing validation may reach `complete`.

Validation reconstructs the exact report sections and Markdown bytes from the current
profile, research, candidate, finalist, scorer, corpus, feature-map, and authoritative
audit artifacts plus the report's hash-bound structured draft specification. The audit is
revalidated with the frozen SimRisk configuration and must contain exactly one matching
result for every current finalist and candidate. Additional, omitted, altered, reordered,
or contradictory report material therefore fails deterministic validation even when it
uses an otherwise valid citation.

Private draft creation and normal completion are not disclosure. A sensitive-disclosure
gate is required only before a report is shared or exported outside its owner-only run.
Approval binds the exact report, recipient, destination, purpose, and sensitive fields and
is consumed once. Redaction creates a new report revision and invalidates review and
validation; its non-plaintext redaction history binds the exact prior report, disclosure
hash, and consumed decision so the redacted bytes can be reconstructed before a new
review. Stop is terminal. An exact retry of a completed share replays its immutable
receipt and export without consuming another approval; any changed scope requires a new
gate.
