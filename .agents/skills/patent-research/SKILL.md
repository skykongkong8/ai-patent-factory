---
name: patent-research
description: Use when the user explicitly invokes $patent-research to start a run and gather bounded research evidence.
---

# Patent research

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Resolve the inventor/topic or fixed run scope through
   [inventor management](../../../docs/inventor-management.md); preserve its IDs
   and keep workspace requests under the selected run.
3. Execute only the requested **research** stage using the shared
   [research procedure](../../../.claude/commands/research.md), resolving its helper
   references through that contract.
4. Report the selected IDs, versions, paths, and result, then return control to
   the user at the contract's boundary.
