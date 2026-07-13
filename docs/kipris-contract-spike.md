# KIPRIS Plus contract spike (G003)

Status: confirmed offline implementation contract, 2026-07-13. This document is a
technical integration record, not a patentability or legal assessment.

## Primary sources

- [Patent/utility-model service page](https://plus.kipris.or.kr/eng/popup/service/DBII_000000000000001/view.do)
- [Current `getWordSearch` API description](https://plus.kipris.or.kr/portal/popup/DBII_000000000000001/SC002/ADI_0000000000002038/apiDescriptionSearch.do)
- [Current bibliography-summary API description](https://plus.kipris.or.kr/portal/popup/DBII_000000000000001/SC002/ADI_0000000000002131/apiDescriptionSearch.do)
- [2024 KIPRIS Plus guide](https://plus.kipris.or.kr/portal/bbs/view.do?bbsId=B0000001&menuNo=210149&nttId=1060&pageIndex=1)
- [API operating status](https://plus.kipris.or.kr/eng/main/apiStatus.do?menuNo=310128)
- [Terms of use](https://plus.kipris.or.kr/portal/main/contents.do?menuNo=200031)
- [Copyright policy](https://plus.kipris.or.kr/portal/main/contents.do?menuNo=200032)

## Confirmed current contract

The implemented current family is HTTPS/XML under
`https://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice`.
Authentication uses the query parameter `ServiceKey`; the repository reads its
value only from `KIPRIS_PLUS_API_KEY`, adds it after request fingerprinting, and
never stores it in envelopes, events, manifests, logs, or exports.

| Capability | Operation | Confirmed request fields | Implemented output |
|---|---|---|---|
| Keyword search | `getWordSearch` | `ServiceKey`, `word`, `year`, `patent`, `utility`, `numOfRows`, `pageNo` | application number, title, filing date, applicant, abstract, IPC/CPC when supplied, response hash, pagination cursor |
| Bibliography summary | `getBibliographySumryInfoSearch` | `ServiceKey`, `applicationNumber` | the same bounded normalized record contract where the response supplies it |

Responses may return HTTP 200 while reporting an application failure with
`successYN=N`. Confirmed examples include result code `10` for an invalid
parameter and `30` for an unregistered service key. The adapter therefore checks
the application status before accepting any item. HTTP 401/403, 429, timeouts,
network failures, malformed or unsafe XML, and oversized bodies are normalized
as adapter failures. A failure creates no evidence record.

The guide notes fewer than 50 calls per second and a 1,000-calls/month allowance
for a free account. Those are external service limits, not local retry permission:
the request envelope additionally caps deadline, bytes, pages, rows, retries, and
total planning calls. API status must be consulted for live operational state.

## Legacy separation

The older `openapi/rest` family uses an `accessKey` schema and is a separate
contract. Its `freeSearchInfo` description is recorded at
`https://plus.kipris.or.kr/portal/popup/DBII_0000000000010162/SC002/ADI_0000000000010162/apiDescriptionSearch.do`;
URL availability remains to be rechecked during an opt-in live smoke. No legacy
path, parameter, sample, or response shape is mixed into `plus-xml-v1`.

## Terms, retention, and fixture policy

The adapter stores normalized public metadata and a SHA-256 response hash, not
raw KIPRIS payloads. Every observation carries the note that raw responses are
not cached or redistributed. Users remain responsible for the official terms and
copyright policy. Repository fixtures are redacted, minimal contract examples;
they do not imply permission to redistribute full source datasets.

## Explicit unknowns and live-smoke boundary

- No current JSON response contract is confirmed; the implementation accepts XML only.
- Exact account-specific quotas, paid-plan behavior, and rate-limit headers are not assumed.
- Response drift beyond the committed fixtures is not guessed; it becomes a malformed failure.
- The legacy URL and any service-side changes require a separately authorized live check.
- A present key is not treated as remotely valid by an offline diagnostic. Simulated-invalid
  and fixture-usable modes are labeled as such and never substitute for live verification.

Ordinary CI is entirely offline. A live smoke is optional, credential-gated,
redacted, separately reported, and never used to weaken fixture-contract tests.

## Evidence identity boundary

Stable evidence identity is the normalized `source_locator` plus the normalized
content revision hash. KIPRIS records use `kr-patent:<normalized-number>` while
the current manual-import contract deliberately retains an allowlisted HTTPS
source URL. Therefore identical titles, identifiers, or content hashes from those
two locator families are not silently collapsed across adapters. They remain two
evidence nodes with separate observations until a later, explicit reconciliation
contract can prove locator equivalence. This limitation avoids title-only or
hash-only collapse and is not broadened into similarity/corpus logic in G003.
