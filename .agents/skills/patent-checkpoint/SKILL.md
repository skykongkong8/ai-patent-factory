---
name: patent-checkpoint
description: Use when the user explicitly invokes $patent-checkpoint to present the audit dossier and apply a user-authored decision.
---

# Patent checkpoint

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Resolve the inventor/topic or fixed run scope through
   [inventor management](../../../docs/inventor-management.md); preserve its IDs
   and keep workspace requests under the selected run.
3. Execute only the requested **checkpoint** stage using the shared
   [checkpoint procedure](../../../.claude/commands/checkpoint.md), resolving its helper
   references through that contract.
4. Report the selected IDs, versions, paths, and result, then return control to
   the user at the contract's boundary.
