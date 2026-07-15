# Claude Code contract

1. Read `README.md`, `SETUP.md`, and this file first.
2. Keep source text and profiles only under `documents/` and `workspace/`.
3. Treat SQLite `workspace/profile.sqlite3` as the only authoritative state.
   `profile.json` is a deterministic export, so do not edit it directly and do not
   bypass `conflict_resolution_required`.
4. Before putting private source text into model context, verify the transfer
   scope. Every command is offline except `research serpapi`, the one opt-in
   networked command: it and its free quota preflight egress only the search
   keyword and the `SERPAPI_API_KEY` value to `serpapi.com`. The key is never
   persisted and is canary-scrubbed from all artifacts. No command sends
   `documents/`, `workspace/`, or profile data to the network.
5. `agent_inference` always requires a `rationale` and must not be expressed as an
   established fact.
6. Do not issue legal conclusions about patentability, novelty, validity, or
   non-infringement/FTO.
7. Use only relative, non-symlink, canonical paths — inputs/responses under the
   configured documents root, and the DB/exports under the configured workspace
   root.

Initialize: `python3 -m patent_factory init`, then `/setup` or
`python3 -m patent_factory profile --help`.
