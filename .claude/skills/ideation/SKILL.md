---
name: ideation
description: Prepare evidence-bound candidate, shortlist, and report inputs while leaving every transition and gate to the JSON CLI core.
---

# Ideation workflow

Use versioned request objects: `candidate-input-v1`, `shortlist-input-v1`, and `report-input-v1`. They are inputs to `ideate`, `shortlist`, and `draft`; they are not authoritative state or substitutes for CLI exports.

Every candidate traces to a user problem/capability and evidence. Preserve all six epistemic labels. `agent_inference` requires rationale; hypotheses and creative suggestions must not become facts. Each finalist needs independent differentiation, technical-feasibility, and utility-significance axes with score, rationale, confidence, supporting and contrary evidence, gaps, and coverage limitations. If three defensible finalists are unavailable, preserve `insufficient_evidence` and stop.

Never directly edit candidate/finalist/report exports or SQLite. Stop on domain pivot, evidence, coverage, excessive-risk, disclosure, revision, and other gates. This skill may recommend but never make the user's pivot or excessive-risk decision. Do not load private inputs into hosted context without a current exact egress approval and minimized manifest.
