# SerpApi Google Patents contract (research serpapi)

Status: implemented integration contract. This document is a technical integration
record, not a patentability or legal assessment.

## Purpose and boundary

`research serpapi` is the **only** command that performs a live network request. It
runs one bounded Google Patents keyword search through SerpApi's official Google
Patents engine, normalizes the results into the same hashed evidence graph as the
KIPRIS and manual-import adapters, and never persists or logs the API key. Every
other command remains offline; ordinary CI is entirely offline (see the offline seam
below).

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
field or `search_metadata.status != "Success"` becomes a normalized adapter failure
and creates no evidence. HTTP 401/403 → `auth`, 429 → `rate_limit`, timeouts and
network errors → normalized failures.

## Quota model and Path-A fallback

The SerpApi free tier allows 250 searches/month. `research serpapi` performs a free
`account.json` preflight and, when `total_searches_left <= --min-quota` (default 1),
**spends no search**: it writes a ready-to-fill `manual_web` import skeleton to
`workspace/requests/manual-web-template.json`, returns `status: quota_exhausted`
(exit code 12), and stops. A SerpApi quota error encountered mid-search normalizes to
a `rate_limit` failure and triggers the same fallback. The fallback never fabricates
or auto-substitutes evidence — the user supplies records and re-runs the offline
`research manual … --allow-host patents.google.com` path.

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
and quota-exhausted fallback — is exercised without any live call. Fixtures live at
`tests/fixtures/google_patents/` and `tests/fixtures/serpapi/`.

## Explicit unknowns and live boundary

- Exact per-plan quotas, rate-limit headers, and response drift beyond the committed
  fixtures are not assumed; drift becomes a normalized `malformed` failure.
- A present key is not treated as valid by the offline diagnostic. Use
  `scripts/check_serpapi_quota.py --live` for a free presence + remaining-count check.
- The live path spends real searches; it is opt-in per invocation and never runs in CI.
