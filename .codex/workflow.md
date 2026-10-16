# Execute one patent workflow stage in Codex

This is the Codex execution contract for the eight `patent-*` skills. Stage
procedures live in `.claude/commands/`; read only the requested command and its
relevant helper. The Python JSON CLI owns state, gates, and exports.

## Execution

1. Confirm the user explicitly requested this stage in the current invocation.
   A prior stage's next-step suggestion, a helper being selected, or a request to
   inspect documents does not authorize execution. If the user requests a chain
   of stages, ask which single stage to execute first. This keeps progression
   under the user's control.
2. Read `README.md`, `SETUP.md`, `CLAUDE.md`, and `AGENTS.md` as required by the
   repository contract. Read the selected skill's linked shared command. Resolve
   its helper references using the table below, including references phrased as
   an "installed" skill; local migrated copies are not procedure authorities.
3. For inventor/profile/topic setup, replacement, or legacy adoption, follow
   [inventor management](../docs/inventor-management.md). Select the inventor and
   topic IDs explicitly; preserve candidate IDs on conflicts. Setup may prepare
   topic context but never starts a run.
   Use paths, run ID, input mode, and other choices already supplied by the user
   or established in this session. For setup with a document path, choose the
   document route directly. When a required choice is absent or several runs or
   inputs are plausible, ask for that choice before mutation. Never select every
   file under `documents/` merely because no input was specified.
4. For an existing run, use its fixed profile/topic versions and keep every
   workspace request or workbench beneath `RUN/requests/`. Scaffolds, including
   report scaffolding, need the same run scope. Inspect it with
   `python3 -m patent_factory run status --run RUN --run-id RUN_ID` before a
   mutation. Let the CLI enforce prerequisites; if an earlier stage is needed,
   report the required stage and stop. Only research bootstraps a user-requested
   new run with `run start`; inspection never creates a run or auto-runs setup.
5. Execute the requested shared procedure through the local CLI, observing the
   boundaries below. Treat repository privacy/gate rules as authoritative when a
   shared helper suggests reading an immutable export or continuing elsewhere:
   use `run status`, `run show`, or `gate inspect` for supported read-only access,
   and stop with the limitation when the required current artifact is unavailable.
6. Return the core's `status` and `next_state`, the selected inventor/topic and
   version IDs, candidate ID when present, run ID/path, any output
   paths and limitations, and the next explicit Codex call. Preserve any
   `gate_id`, `subject_revision_hash`, `actions`, and `next_state` exactly as
   returned. End the invocation; a suggestion is not an execution request.

## Boundaries within a stage

- Completion of setup, research, ideate, shortlist, audit, checkpoint, or draft
  always returns control to the user. Translate `/stage` next-step suggestions
  from the shared commands to `$patent-stage`; never invoke the suggested stage.
- Research performs the chosen bounded operation. Once it returns
  `research_complete`, stop; a shared helper's suggestion to repeat imports per
  source does not permit another research operation from that completed state.
- Ideate preserves the shared command's bounded invocations. Scaffold the brief
  and stop; on a fresh `$patent-ideate`, prepare/validate divergence and stop;
  then prepare/validate entanglement and stop; then scaffold the promoted
  candidate input and stop; then fill/validate promotion and stop; then publish
  on a fresh invocation. Use the latest recorded CLI
  result and supported validators to identify the next unfinished boundary. If
  progress is unclear, ask where to resume rather than publish or skip checks.
- Audit stops on the always-raised post-audit `decision_required` gate, including
  a clean score. Checkpoint needs the user's exact authored decision input; it
  never invents approval, reasons, feedback, or decision IDs. Even after an
  approving decision, report `$patent-draft` and stop. Re-ideate/re-research
  decisions similarly return their next call without executing it.
- Review includes the existing independent reviewer pass and deterministic
  validation only after `reviewed`. On `revision_required`, return the draft
  handoff and stop. A bare `$patent-review` never shares a report. External share
  is a separate user-requested operation under the existing disclosure gate.
- All `*_required`, `coverage_insufficient`, `decision_required`,
  `insufficient_evidence`, `revision_required`, `stopped`, and `error` results
  return control immediately. A fresh stage invocation resumes an interrupted
  operation only with the exact original input and any core-issued decision ID.

## Shared helper routing

Read the relevant helper when its shared command requires it. These are reference
procedures, not additional user commands or permission to execute another stage.

| Requested stage | Shared helper |
| --- | --- |
| setup | [profile](../.claude/skills/profile/SKILL.md) |
| research, audit | [research](../.claude/skills/research/SKILL.md) |
| ideate, shortlist, draft | [ideation](../.claude/skills/ideation/SKILL.md) |
| checkpoint | [checkpoint](../.claude/skills/checkpoint/SKILL.md) |
| review | [patent-review](../.claude/skills/patent-review/SKILL.md) |

## Privacy

Local execution is distinct from hosted-model access. Passing an input path to
the local CLI does not authorize reading its contents into Codex. Follow the
exact approval, minimized egress manifest, and secret-handling requirements in
`AGENTS.md` before any private source/profile/evidence/report enters context.
Skill invocation itself grants no disclosure or network approval.
