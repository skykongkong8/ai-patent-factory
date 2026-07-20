"""Bounded public-source adapters; the only research network boundary."""

from .base import SearchAdapter, TransportResponse
from .google_patents import GooglePatentsAdapter, serpapi_account
from .kipris import KiprisAdapter
from .manual_web import ManualWebAdapter

__all__ = [
    "GooglePatentsAdapter",
    "KiprisAdapter",
    "ManualWebAdapter",
    "SearchAdapter",
    "TransportResponse",
    "serpapi_account",
]
