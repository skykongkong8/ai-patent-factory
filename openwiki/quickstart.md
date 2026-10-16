---
type: "Reference"
title: "AI Patent Factory: Agent Quickstart"
openwiki_generated: true
verified:
  - by: openwiki/0.6.0
    at: 2026-10-16T03:36:32.329Z
sources:
  - id: openwiki-source-8037e2358a2c4f9b2c722a11
    resource: repo://AGENTS.md
  - id: openwiki-source-a2371d6362e5db4bc834ad03
    resource: repo://CLAUDE.md
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-5096bfb21b732fa2f0beecbd
    resource: repo://SETUP.md
  - id: openwiki-source-c1a9676b43a2a0c748e6207b
    resource: repo://src/patent_factory/cli.py
  - id: openwiki-source-ef94c4f6ce7ef5deb426b558
    resource: repo://src/patent_factory/state.py
generated: { by: "openwiki/0.6.0", at: "2026-10-16T03:36:32.329Z" }
---


# AI Patent Factory: Agent Quickstart

This is the maintainer and coding-agent entrypoint, not a replacement for the CLI reference. Start with the system boundary below, then use the **narrowest linked page** for the behavior you intend to change. Read source and tests as the final authority.

## Non-negotiable operating boundary

The local Python CLI/core (`python3 -m patent_factory`) is authoritative for workflow **state, gates, invalidation, idempotency, and exports**. Slash commands, Codex skills, scripts, and other agent wrappers may guide a user, prepare a request, invoke the CLI, and explain its JSON result; they must not become a second state engine.

- Do **not** directly edit SQLite databases, `profile.json`, immutable exports, artifact pointers, or share receipts. Do not use SQL, file copying, or arbitrary scripts to create a transition, repair a run, approve a gate, or publish an artifact. Use the supported CLI inspection and cleanup surfaces instead.
- A scaffolded request and the ideation workbench are inputs, not authority. Hash bindings and schemas are rechecked by the core when a stage publishes its artifact.
- A gate result is a hard stop. Preserve its `gate_id`, subject hash, offered actions, and next state. Never auto-approve it or invent a decision, approval scope, reason, or `decision_id`; only a user-authored decision for the exact current subject and scope can be accepted by the core.
- Parse the CLI's JSON envelope—especially `status`, `next_state`, identifiers, and `failure_code`—rather than inferring state from a file or treating every nonzero exit as a crash. Except for `--help` and `--version`, the CLI produces one versioned JSON object even for handled argument errors.

> **Privacy and legal limit.** Private source text and profile text remain local unless there is exact current approval for the external recipient, purpose, approved data class/content hash, and scope, together with a minimized egress manifest. A local CLI invocation or use of a hosted coding agent is not approval to put private material in model context. The factory organizes invention material and records bounded similarity research; it must not make legal conclusions about patentability, novelty, inventive step, validity, or non-infringement/FTO.

## Orient before changing anything

1. Read [`AGENTS.md`](../AGENTS.md), [`README.md`](../README.md), [`SETUP.md`](../SETUP.md), and [`CLAUDE.md`](../CLAUDE.md). Confirm the installed contract with `python3 -m patent_factory --version` and `python3 -m patent_factory --help`.
2. Classify the change by its **authority boundary**: core state/provenance, private inputs and CLI behavior, a workflow stage, a network adapter, privacy/egress, or verification/recovery. Use the routing table rather than reading unrelated pipeline code.
3. Trace the public CLI verb to its core handler and state transition, then read the closest integration test before editing. For a change to a stage input, trace the scaffold, schema, validator, artifact dependencies, invalidation effects, and the resume/gate path—not just the parser argument.
4. Make the smallest change that keeps the core authoritative. If a behavior needs a new workflow capability, design the validated core operation, state transition, artifact/dependency bindings, gate policy, and tests before adding guidance in a wrapper.

Normal use begins by creating private `documents/` and `workspace/` roots. The profile database is authoritative for profile facts; each started run has its own `factory.sqlite3` for the run lifecycle, artifacts, evidence, decisions, and exports. Use `run status`, `run show`, and `gate inspect` to observe current state rather than reading the databases or exports directly.

## End-to-end mental map

The guided pipeline is `/setup` → `/research` → `/ideate` → `/shortlist` → `/audit` → `/checkpoint` → `/draft` → `/review`.

1. **Profile and run bootstrap:** private documents or an interview become conflict-checked profile facts; `run start` binds a current profile into a new research-ready run.
2. **Evidence:** research persists bounded fixture/manual evidence or uses an explicitly credentialed live adapter. Adapter failures and thin coverage remain limitations; they are not evidence.
3. **Ideas and finalists:** the divergence-first workbench is deliberately non-authoritative until validated promoted input is submitted. Candidates must bind profile facts and research evidence; shortlisting produces three scored finalists or an explicit insufficiency outcome.
4. **Similarity audit and human checkpoint:** finalist-specific retrieval creates the audit corpus, then scoring raises the post-audit checkpoint even when the score is clean. The human chooses `approve`, `re_ideate`, `re_research`, or `stop`; no wrapper may skip directly to drafting.
5. **Private report and controlled release:** a bound report is independently reviewed. `revision_required` returns to drafting; only a reviewed report can be deterministically validated and completed. External sharing is a separate exact-scope disclosure operation, not a copy of a completed report.

This lifecycle is a dependency graph as well as a sequence. Replacing an upstream current artifact invalidates dependent revisions and makes approvals tied to affected hashes stale. A repeated idempotency coordinate replays its recorded current result; it does not authorize a second publication.

## Route the change to the narrowest page

| If the change concerns… | Read this page first | Then trace / prove |
| --- | --- | --- |
| Run states, transactions, SQLite schema/migrations, artifact revisions, dependency invalidation, gates, atomic exports, or idempotency | [Transactional State, Artifact DAG, and Gates](/openwiki/architecture/state-and-provenance-kernel.md) | The transition/gate policy and transactional-kernel tests, including fault and replay cases. |
| The division between slash-command/Codex guidance, the JSON CLI, local roots, and service boundaries | [System Overview and Runtime Boundaries](/openwiki/architecture/system-overview.md) | The relevant CLI handler and the wrapper or skill that invokes it. |
| Private roots, contained paths, CLI envelopes, scaffolds, request files, status/exit handling, run inspection, or safe cleanup | [CLI Surface, Private Workspace, and Scaffolded Inputs](/openwiki/operations/cli-private-workspace-and-scaffolds.md) | Path validation and the exact request/schema contract; do not hand-copy bindings. |
| KIPRIS Plus, SerpApi Google Patents, manual web imports, credential handling, request budgets, host allowlists, normalization, quotas, or adapter failures | [Research Adapters and Network Egress](/openwiki/integrations/research-adapters-and-network-egress.md) | The adapter contract, query envelope, persisted observation/failure mapping, and egress-before-state checks. |
| Profile document/interview intake, fact provenance, conflicts, deterministic profile export, or profile-to-run binding | [Profile Ingestion and Run Bootstrap](/openwiki/workflows/profile-and-run-bootstrap.md) | Conflict resolution and the authoritative-profile verification performed before a run starts. |
| Fixture/manual/live research, evidence persistence, bundle publication, incompleteness, or a supported research re-entry | [Research Intake, Persistence, and Re-entry](/openwiki/workflows/research-and-evidence-intake.md) | Research-state permissions, idempotency coordinates, and any credential/re-entry gate. |
| Hash-only ideation briefs, workbench divergence/entanglement/promotion, candidate publication, domain pivots, or finalist selection | [Ideation Workbench, Candidate Publication, and Shortlisting](/openwiki/workflows/ideation-and-finalist-selection.md) | The distinction between non-authoritative workbench files and core-published candidates/finalists. |
| Audit query inputs, finalist-specific corpora, reviewed feature maps, scoring, coverage outcomes, or the mandatory post-audit decision | [Similarity Audit and Post-Audit Human Checkpoint](/openwiki/workflows/similarity-audit-and-human-checkpoint.md) | The checkpoint branch and the exact resolution artifact, not an inferred “clear” score. |
| Report bindings/rendering, reviewer independence, deterministic validation, redaction/revision loops, or exact-scope sharing | [Report Publication, Independent Review, Validation, and Sharing](/openwiki/workflows/report-review-validation-and-sharing.md) | The current report/review/validation bindings and, for sharing, the sensitive-disclosure decision. |
| Hosted-model use, sensitive data classes, credentials, canary scrubbing, disclosure approval, or the legal boundary | [Privacy, Authority, and Legal Boundaries](/openwiki/concepts/privacy-and-authority-boundaries.md) | Whether any contemplated transfer has exact approval; absent that, keep the work local. |
| Which unit, integration, end-to-end, or release-evidence tests should change with an implementation | [Verification Strategy and High-Value Test Boundaries](/openwiki/testing/verification-strategy.md) | The focused regression first, then the documented offline release suite. |
| Crash recovery, concurrent publication, replay versus retry, run deletion, or release/calibration evidence | [Reliability, Recovery, and Release Evidence](/openwiki/operations/reliability-recovery-and-release-evidence.md) | Fault-injection/concurrency coverage and the recorded recovery result. |

## Safe agent loop

**Before invoking a mutating stage:** obtain the current run state through the CLI, use a scaffold when one exists, and keep inputs under their configured private root. Treat the scaffold's hashes as values to preserve, not fields to regenerate creatively.

**After each invocation:** retain the full JSON result. A complete stage may advance normally; `conflict_resolution_required`, `credential_required`, `domain_pivot_required`, `insufficient_evidence`, `coverage_insufficient`, `decision_required`, `revision_required`, `sensitive_disclosure_required`, `stopped`, `incomplete`, and `error` each require the behavior named by their workflow page. In particular, every `audit score` normally stops at `decision_required` for the post-audit checkpoint.

**When a gate is pending:** inspect the exact gate, return control for the required human decision, then resume only as the core directs with the exact returned `decision_id` and the same bound input. Content or scope changes deliberately invalidate older approvals.

**When implementation changes:** preserve deterministic serialization and the JSON envelope, add or update focused tests at the affected boundary, and run the repository's documented offline verification before release. Do not test live credentials by default, and do not report a `pytest` run as the project suite: the repository's standard-library `unittest` discovery is the authoritative test command.

## Fast escalation rules

- **It looks like a state problem:** stop editing files and route to the state/provenance page. An apparent export mismatch, stale input, or retry should be resolved through the core's inspection/replay/recovery behavior.
- **It needs private text in an agent or external system:** stop unless the exact transfer approval and minimized manifest already exist; consult the privacy page before discussing implementation details in an external context.
- **It needs an approval to continue:** stop. A status or an agent recommendation is never approval.
- **It looks legally decisive:** relabel it as bounded research evidence or similarity within the retrieved corpus and route the decision to a qualified patent professional. The system must not turn it into a patentability, novelty, validity, or non-infringement/FTO conclusion.

For raw syntax and examples, use the operational CLI page and [`SETUP.md`](../SETUP.md). For a behavioral change, prefer the linked architecture or workflow page plus its focused tests over copying a command sequence from this quickstart.
