# AI Patent Factory

*A local-first, Korean-first workflow for turning your own inventions into a rigorous, evidence-bound invention report — driven from Claude Code slash commands.*

Everything runs on your machine. The private Python CLI (`patent_factory`) is the
single source of truth for state, gates, and exports; Claude Code drives it
through guided slash commands so you never have to remember raw arguments. Your
documents never leave your disk unless *you* explicitly approve an external share.

> This tool produces **invention-organizing** material, not legal advice. It never
> concludes patentability, novelty, inventive step, validity, or non-infringement/FTO.
> See [Legal boundary](#legal-boundary).

## What this is / who it's for

You are an inventor (or working with one) who wants to move from a pile of private
notes to a structured, citation-bound Korean invention report — without silently
inventing facts or claims. The workflow is deliberately **gated**: every stage
either produces evidence-bound output or stops at a gate and asks *you* to decide.
Claude proposes and assembles; the CLI validates, binds hashes, and records state.

If you have used the reference project [`ai-job-search`](https://github.com/MadsLorentzen/ai-job-search),
the shape will feel familiar: **slash commands are the interface**, each command
tells you where to put your input and what happens next, and the raw CLI stays
available as an escape hatch.

## The pipeline

```
  /setup   →   /research   →   /ideate   →  /shortlist  →   /audit   →   /draft   →   /review
    │             │              │              │              │            │            │
    ▼             ▼              ▼              ▼              ▼            ▼            ▼
  build         gather        propose ≥3     pick 3         score        render       independent
  inventor      bounded       evidence-      finalists      similarity   Korean       reviewer pass,
  profile       KIPRIS +      bound          on 3 axes      risk vs      11-section   validate, and
  facts         local         candidates     each          KIPRIS       report       guarded share
                evidence                                     corpus
```

- Run **`init` once first** (below) to create your private roots.
- **`/research`** also performs the one-time `run start` that binds your profile
  into a fresh run.
- **`/review`** also covers deterministic `validate` and the guarded external
  `share`.
- At every stage, a `*_required` / `insufficient_evidence` / `coverage_insufficient`
  gate means *stop and decide*, never *auto-proceed*. See
  [How it works](#how-it-works).

## Prerequisites

- **CPython 3.11+**. No third-party runtime packages.
- A terminal (the CLI is offline; only an optional KIPRIS lookup uses the network,
  and only after you approve it).

```bash
python3 -m patent_factory --version
python3 -m patent_factory init
```

`init` creates two owner-only roots and nothing else:

- `documents/` — your **private input** (background notes, references, interview
  answers). See [`documents/README.md`](documents/README.md).
- `workspace/` — **generated state and exports** (the authoritative SQLite DB, the
  profile export, and per-run artifacts). See [`workspace/README.md`](workspace/README.md).

Keep all source and profile data inside these two roots. Everything under them
except the READMEs is git-ignored.

## Quick start (from Claude Code)

1. **Add your material.** Drop background notes and references into `documents/`
   (UTF-8 `.md` / `.txt` / `.json`). Formats and options are in
   [`documents/README.md`](documents/README.md).
2. **`/setup`** — build your inventor profile from one of three input paths
   (a folder, a single document, or an interview). Safe to re-run.
3. **`/research`** — start a run and gather bounded evidence (offline `fixture`
   acceptance path, or user-supplied `manual` results).
4. **`/ideate`** → **`/shortlist`** — propose evidence-bound candidates, then pick
   three defensible finalists.
5. **`/audit`** — score each finalist's similarity risk against a KIPRIS corpus.
6. **`/draft`** → **`/review`** — render the Korean 11-section report, then run an
   independent review, `validate`, and (optionally) a guarded `share`.

Each command tells you exactly which input it needs, where to put it, and which
command comes next. You don't call Python directly for any of this.

## Commands

| Command | Stage | What it does |
| --- | --- | --- |
| `/setup` | Profile | Build/enrich the private inventor profile (folder · document · interview). Idempotent. |
| `/research` | Evidence | Bind the profile into a run and gather bounded fixture/manual evidence. |
| `/ideate` | Candidates | Validate and persist ≥3 evidence-bound candidate proposals. |
| `/shortlist` | Finalists | Persist 3 finalists (each scored on 3 independent axes) or explicit insufficient evidence. |
| `/audit` | Similarity | Retrieve finalist-specific KIPRIS corpora and score `simrisk-v1.0.0` risk. |
| `/draft` | Report | Render the private Korean 11-section report with citation/decision bindings. |
| `/review` | Review + release | Independent reviewer pass, deterministic `validate`, and guarded external `share`. |

Cross-cutting CLI verbs the commands use for you: `gate inspect` / `gate decide`
(handle one exact gate) and `delete-run` (safely remove one run). All are
documented in [SETUP.md](SETUP.md).

## Where your files live

```
documents/            # PRIVATE input you provide (git-ignored except README)
  README.md           #   → format guide: what to put here and how
workspace/            # generated state + exports (git-ignored except README)
  README.md           #   → outputs guide + versioned request templates
  profile.sqlite3     #   authoritative profile state (never hand-edit)
  profile.json        #   deterministic export of the DB (never hand-edit)
  requests/           #   the versioned *-input-v1.json you author for each stage
  runs/<RUN>/         #   per-run DB + research/report exports
.claude/
  commands/           # the 7 slash commands above
  skills/             # profile · research · ideation · patent-review workflows
SETUP.md              # install + full raw-CLI reference (the escape hatch)
CLAUDE.md, AGENTS.md  # the agent contract (authority, gates, privacy, no legal calls)
schemas/, templates/  # JSON Schemas for each input; the Korean report template
```

## How it works

What makes the output trustworthy is the same set of rules the reference project
leans on — adapted for a patent workflow where correctness matters more than speed:

- **The CLI is the only authority.** State transitions, gate decisions,
  invalidation, idempotency, and exports happen *only* in the Python core. Claude
  never edits `profile.sqlite3`, `profile.json`, or any export, and never fakes a
  decision by copying a file.
- **Everything is evidence-bound and hash-bound.** Candidates trace to profile
  facts and research evidence; finalists bind to candidate revisions; the report
  binds to an approved artifact hash. Change the content and the old gate no longer
  applies.
- **Gates stop for a human.** `*_required`, `coverage_insufficient`,
  `decision_required`, `insufficient_evidence`, `revision_required`, `stopped`, and
  `error` are hard stops. Resolving one needs a *user-authored*
  `gate-decision-input-v1` and the core-issued `decision_id` — Claude cannot invent
  approval, scope, or reasons.
- **Drafter ≠ reviewer.** The report is drafted and then reviewed in a separate
  pass with a different identity before deterministic `validate` can complete it.
- **Privacy is explicit.** Running the local CLI does not authorize sending your
  private text to a hosted model or anyone else. External sharing is a separate,
  gated `share` operation with an exact recipient, purpose, and content hash.
- **Determinism.** Given the same committed state, exports regenerate byte-for-byte;
  the offline `fixture` research path needs no network or credentials.

## Advanced / manual (raw CLI)

Slash commands are the recommended surface, but every stage is a plain
`python3 -m patent_factory <verb> …` call underneath. If you are scripting, using a
runtime without slash commands (e.g. Codex — see [`.codex/README.md`](.codex/README.md)),
or debugging, the **full CLI reference lives in [SETUP.md](SETUP.md)**: install,
the three profile paths, input formats and path-safety rules, the
`KIPRIS_PLUS_API_KEY` credential check, and the research/audit/report/share verbs
with their state transitions.

## Legal boundary

The output of this tool is invention-organizing support material, **not legal
advice**. It does not provide legal conclusions about patentability, novelty,
inventive step, validity, or non-infringement/FTO. Similarity scores are a research
aid within a retrieved corpus only. Confirm any decision that matters with a
qualified patent attorney or agent.
