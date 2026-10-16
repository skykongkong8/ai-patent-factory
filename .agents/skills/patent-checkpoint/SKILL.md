---
name: patent-checkpoint
description: Use when the user explicitly invokes $patent-checkpoint to present the audit dossier and apply a user-authored decision.
---

# Patent checkpoint

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Execute only the requested **checkpoint** stage using the shared
   [checkpoint procedure](../../../.claude/commands/checkpoint.md), resolving its helper
   references through that contract.
3. Report the result and return control to the user at the contract's boundary.
