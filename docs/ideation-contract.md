# G004 ideation and shortlist contract

G004 is a deterministic local validation and persistence boundary. It does not call a
model, search the network, calculate simrisk-v1.0.0, or make excessive-similarity
decisions.

The ideate command accepts private candidate-input-v1 JSON plus the canonical profile
export and its authoritative profile SQLite database. The core requires an exact
database/export match and imports a run-scoped current profile_context artifact before
candidate publication; forged, cross-run, or stale profile inputs fail before mutation.
Every candidate must
describe the problem, mechanism, inputs, components, interactions, transformations,
outputs, expected effects, implementation example, measurable validation, unresolved
dependencies/questions, and one explicit modify, combine, adapt, constrain, or
transfer synthesis trace. It must reference both profile problem/capability claims
and current research evidence revisions. Exact source spans are required when
available; a missing span requires an explicit limitation. The core adds the current
profile, research-bundle, and evaluation-config hashes and derives a stable ca_
identifier. Profile references use field plus claim ID plus category, so real
interview facts may share one provenance claim ID while remaining distinct facts.

The shortlist command accepts shortlist-input-v1. A finalist is structurally
defensible only when differentiation, technical_feasibility, and utility_significance
are present. Each axis has a 0–100 research-aid score, rubric version, rationale,
confidence, supporting and contrary evidence, gaps, and coverage
assessment/limitations. G004 uses no numeric finalist threshold and does not average
these axes. Priorities are ordered deterministically by priority then candidate ID.

Three or more unique complete finalists produce immutable finalist-set-v1 and state
finalists_ready. Fewer than three produce immutable insufficiency-v1, state
insufficient_evidence, explicit eligible/rejected IDs, reason codes, missing evidence,
limitations, unresolved questions, and recommended research. That branch creates no
finalist pointer.

All inputs and exports remain under the configured private workspace. Candidate and
finalist exports are registered through the authoritative SQLite transaction/recovery
path. A changed upstream revision makes dependent artifacts stale. A domain change
is detected before candidate persistence and enters domain_pivot_required. Its gate
subject is a content-addressed ideation request and its scope includes the candidate
input fingerprint plus exact profile, research, and evaluation-config hashes, so a
changed request cannot reuse approval. G006 owns the later decision and resumption
behavior.
