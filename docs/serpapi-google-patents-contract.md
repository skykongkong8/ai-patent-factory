# SerpApi Google Patents contract (research serpapi)

Status: implemented integration contract. This document is a technical integration
record, not a patentability or legal assessment.

## Purpose and boundary

`research serpapi` is the **only** command that reaches SerpApi, and the only one
that leaves the KIPRIS host allowlist. It runs one bounded Google Patents keyword
search through SerpApi's official Google Patents engine, normalizes the results
into the same hashed evidence graph as the KIPRIS and manual-import adapters, and
never persists or logs the API key. The repository's other networked paths —
`research kipris` and `audit retrieve --live` — reach `plus.kipris.or.kr` with
`KIPRIS_PLUS_API_KEY` instead; every remaining command is offline, and ordinary CI
is entirely offline (see the offline seam below).

## Endpoints

- Search: `https://serpapi.com/search` with `engine=google_patents` (HTTPS/JSON).
- Account/quota: `https://serpapi.com/account.json` (free; does not consume a search).

Authentication uses the query parameter `api_key`; the repository reads its value
only from `SERPAPI_API_KEY`, adds it after request fingerprinting, and never stores
it in envelopes, events, manifests, logs, or exports. The value is also removed from
error strings and asserted absent from every stored artifact via the shared
`credential_canaries()` leak check.

| Capability | Operation | Confirmed request fields | Implemented output |
|---|---|---|---|
| Keyword search | `engine=google_patents` | `api_key`, `q`, `num` (10–100), `page`, `output=json`, optional `country`/`language`/`status`/`type`/`before`/`after`/`sort` | publication number, title, filing/priority date, assignee, snippet abstract, canonical Google Patents URL, response hash, pagination cursor |

The adapter parses `organic_results[]`; pagination and totals come from
`search_information.total_results` and `serpapi_pagination.next`. A top-level `error`
field or any `search_metadata.status` other than a final `"Success"` (including
`"Processing"`, a missing status, or a non-object container) becomes a normalized
adapter failure and creates no evidence. HTTP 401/403 → `auth`, 429 → `rate_limit`,
timeouts and network errors → normalized failures. Two closed marker lists split
the `error` bodies that are recoverable rather than malformed: monthly-allowance
phrases ("run out of searches", "plan searches", "monthly search") and throttle
phrases ("too fast", "throughput", "hourly", "per second", "too many requests",
"slow down"). Both normalize to `rate_limit` because the taxonomy has no separate
throttle kind, but they carry distinct messages ("monthly search quota exhausted"
vs. "throttled the request rate") so a throttle is offered a retry instead of a
manual-import handoff; anything else stays `malformed`. A `patent_link` is accepted as
the canonical URL only when it is a canonical HTTPS `patents.google.com` URL;
anything else falls back to the URL constructed from the validated publication
number. The priority date is never substituted for a missing filing date; the
substitution-free absence is recorded as a record limitation instead.

## Quota model and Path-A fallback

The SerpApi free tier allows 250 searches/month. `research serpapi` validates the
run and its state in the authoritative run database first, then performs a free
`account.json` preflight and, when `total_searches_left <= --min-quota` (default 1),
**spends no search**: it writes a ready-to-fill `manual_web` import skeleton to
`documents/requests/manual-web-template.json` (under the documents root, so the
offline `research manual` command can consume it directly), records the stop as a
`research_quota_stop` artifact revision in the run database, returns
`status: quota_exhausted` (exit code 12), and stops. A template the user has
already edited is preserved, never overwritten. Unedited `REPLACE_WITH_*`
placeholders (including the all-zeros `content_hash` sentinel) are rejected by
`research manual` on import, so the skeleton can never enter the evidence graph.

A `rate_limit` failure encountered mid-search is reported as quota exhaustion
**only when the free account endpoint confirms it**; otherwise the run is reported
as an incomplete research attempt (exit code 4) with a `rate_limit_note` — a
transient per-second/per-hour throttle never fabricates a quota state. Failed
attempts are retried under an automatically advanced idempotency key (`…-r2`,
`…-r3`, …), so a stored transient failure is never replayed as the current
result; a key already used by a credential-decision-bound attempt likewise
advances instead of being silently reused. Resuming with `--decision-id` reuses
the exact key the decision is bound to, and an explicit `--idempotency-key`
keeps exact replay semantics, including stored failures. Replays — successes and
failures alike — never touch the network: no search, no account re-check, no
quota conversion. A fresh attempt is refused before the preflight (exit code 2)
when the run's state does not legally permit research, so refused operations
never egress the credential and never produce quota-stop artifacts. The fallback
never fabricates or auto-substitutes evidence — the user supplies records and
re-runs the offline `research manual … --allow-host patents.google.com` path
(the exit-12 message prints the full command, including the configured
documents/workspace roots, with shell-safe quoting).

Missing or rejected credentials suspend the standard credential gate
(`status: credential_required`, exit code 13); resume with the exact `--decision-id`.

## Evidence identity boundary

Google Patents records use the locator family `gpatent:<normalized-publication-number>`
and `source_type="google_patent"`, deliberately distinct from KIPRIS
`kr-patent:<number>` and from the manual-import canonical HTTPS URL. The same
publication surfaced via different sources is therefore **not** silently collapsed by
identical titles, identifiers, or content hashes until an explicit reconciliation
contract can prove locator equivalence.

## Terms, retention, and fixture policy

The adapter stores normalized public metadata and a SHA-256 response hash, not raw
SerpApi/Google Patents payloads. Users remain responsible for the SerpApi and Google
Patents terms of use. Repository fixtures are redacted, minimal contract examples and
do not imply permission to redistribute full datasets.

## Offline CI seam

`research serpapi` accepts hidden `--fixture-response` and `--fixture-account` paths
(contained under the documents root) that inject deterministic transports instead of
the network, so the full handler — quota preflight, gate flow, evidence persistence,
and quota-exhausted fallback — is exercised without any live call. The two seams are
all-or-nothing: supplying only one is rejected, because a half-configured seam would
silently route the other half of the command (and the real credential) to the live
endpoint. Fixtures live at `tests/fixtures/google_patents/` and
`tests/fixtures/serpapi/`.

## Explicit unknowns and live boundary

- Exact per-plan quotas, rate-limit headers, and response drift beyond the committed
  fixtures are not assumed; drift becomes a normalized `malformed` failure.
- A present key is not treated as valid by the offline diagnostic. Use
  `scripts/check_serpapi_quota.py --live` for a free presence + remaining-count check.
- The live path spends real searches; it is opt-in per invocation and never runs in CI.
