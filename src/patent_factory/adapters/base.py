from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Protocol

from patent_factory.models import AdapterResult, QueryEnvelope


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


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
