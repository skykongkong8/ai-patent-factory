from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from patent_factory.models import AdapterResult, QueryEnvelope


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str | None = None


class SearchAdapter(Protocol):
    name: str
    version: str

    def search(self, envelope: QueryEnvelope) -> AdapterResult: ...


def normalized_patent_number(value: str) -> str:
    """Normalize presentation punctuation without collapsing distinct identities."""

    return re.sub(r"[^0-9A-Za-z]", "", value).upper()


def bounded_body(body: bytes, budget: int) -> bytes:
    if len(body) > budget:
        raise OverflowError("response exceeds byte budget")
    return body


_DATE_SEPARATORS = re.compile(r"[.\-/\s]")


def canonical_date(value: str | None) -> str | None:
    """Canonicalize a retrieved date to YYYY-MM-DD, or return it unchanged.

    Issue #41, reproduced against recorded bytes: the same patent
    (1020160062884) is returned by KIPRIS `word_search` with
    `applicationDate` `20160523` and by `bibliography_summary` as `2016.05.23`.
    Both feed `digest(normalized_record)`, so one patent yielded two
    `content_hash` values and dedup missed it. Google Patents supplies a third
    form, `2020-11-20`.

    Anything not recognizable as an 8-digit date is returned untouched rather
    than guessed at — a wrong canonicalization would silently merge two distinct
    references, which is worse than leaving one unnormalized.
    """

    if value is None:
        return None
    digits = _DATE_SEPARATORS.sub("", value.strip())
    if len(digits) != 8 or not digits.isdigit():
        return value
    year, month, day = digits[:4], digits[4:6], digits[6:]
    if not ("1" <= year[0] <= "2" and "01" <= month <= "12" and "01" <= day <= "31"):
        return value
    return f"{year}-{month}-{day}"


def recording_transport(
    inner: Callable[[str, float, int], TransportResponse],
    destination: Path,
    *,
    canaries: Sequence[str] = (),
    pins: Sequence[tuple[bytes, bytes]] = (),
) -> Callable[[str, float, int], TransportResponse]:
    """Wrap a transport so the exact response bytes are written to disk.

    Recording has to happen *here*, at the transport boundary, because it is the
    only place the raw bytes exist: `live_kipris_smoke.py` drives the CLI by
    subprocess and never sees a body, and the adapters parse before returning.
    Without this there is no way to turn a live call into a fixture, which is why
    every adapter fixture in this repo except one was hand-authored.

    `canaries` are scrubbed from the recorded bytes so a credential can never
    reach a committed fixture. `pins` replace volatile fields (server-side
    timestamps, request ids) so the recording is byte-stable on replay.

    The response is returned unmodified — recording must not perturb the run it
    is observing.
    """

    def transport(url: str, timeout: float, byte_budget: int) -> TransportResponse:
        response = inner(url, timeout, byte_budget)
        recorded = response.body
        for canary in canaries:
            if canary:
                recorded = recorded.replace(canary.encode(), b"[REDACTED]")
        for pattern, replacement in pins:
            recorded = re.sub(pattern, replacement, recorded)
        for canary in canaries:
            if canary and canary.encode() in recorded:
                raise ValueError("recording_transport: credential survived scrubbing")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(recorded)
        destination.chmod(0o600)
        return response

    return transport
