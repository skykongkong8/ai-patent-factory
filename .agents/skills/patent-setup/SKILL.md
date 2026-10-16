---
name: patent-setup
description: Use when the user explicitly invokes $patent-setup to initialize or enrich the inventor profile.
---

# Patent setup

1. Read and follow the [Codex execution contract](../../../.codex/workflow.md).
2. Resolve the inventor/topic or fixed run scope through
   [inventor management](../../../docs/inventor-management.md); preserve its IDs
   and keep workspace requests under the selected run.
3. Execute only the requested **setup** stage using the shared
   [setup procedure](../../../.claude/commands/setup.md), resolving its helper
   references through that contract.
4. Report the selected IDs, versions, paths, and result, then return control to
   the user at the contract's boundary.
