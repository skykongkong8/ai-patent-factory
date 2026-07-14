from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class EpistemicLabel(StrEnum):
    SOURCE_FACT = "source_fact"
    USER_STATEMENT = "user_statement"
    SOURCE_INFERENCE = "source_inference"
    AGENT_INFERENCE = "agent_inference"
    HYPOTHESIS = "hypothesis"
    CREATIVE_SUGGESTION = "creative_suggestion"


class SourceRepresentation(StrEnum):
    """Distinguish exact source text from an interpretation of it."""

    QUOTE = "quote"
    INTERPRETATION = "interpretation"


def normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.strip().replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize(value[key]) for key in sorted(value)}
    return value


def strict_json_loads(payload: str | bytes | bytearray) -> Any:
    """Parse JSON without the standard decoder's silent duplicate-key overwrite."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        normalized_keys: set[str] = set()
        for key, value in pairs:
            normalized_key = normalize(key)
            if normalized_key in normalized_keys:
                raise ValueError("duplicate JSON object key")
            normalized_keys.add(normalized_key)
            result[key] = value
        return result

    return json.loads(payload, object_pairs_hook=unique_object)


def canonical_json(value: Any) -> str:
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_revision_id(prefix: str, source_locator: str, content_hash: str) -> str:
    """Return a retrieval-date-independent content revision identifier."""

    normalized_prefix = normalize(prefix)
    if not isinstance(normalized_prefix, str) or not normalized_prefix.isascii() or not normalized_prefix.isalpha():
        raise ValueError("revision prefix: ASCII letters required")
    locator = normalize(source_locator)
    content = normalize(content_hash)
    if not locator or not content:
        raise ValueError("revision identity: source_locator and content_hash required")
    return f"{normalized_prefix.lower()}_{digest({'source_locator': locator, 'content_hash': content})[:16]}"


def evidence_revision_id(source_locator: str, content_hash: str) -> str:
    return stable_revision_id("ev", source_locator, content_hash)


@dataclass(frozen=True)
class Claim:
    label: EpistemicLabel
    source_id: str | None = None
    content_hash: str | None = None
    span_hash: str | None = None
    rationale: str | None = None
    representation: SourceRepresentation | None = None

    def resolved_representation(self) -> SourceRepresentation:
        if self.representation is not None:
            return self.representation
        if self.label in {EpistemicLabel.SOURCE_FACT, EpistemicLabel.USER_STATEMENT}:
            return SourceRepresentation.QUOTE
        return SourceRepresentation.INTERPRETATION

    def validate(self, path: str = "claim") -> None:
        if self.label in {EpistemicLabel.SOURCE_FACT, EpistemicLabel.SOURCE_INFERENCE}:
            missing = [name for name in ("source_id", "content_hash", "span_hash") if not getattr(self, name)]
            if missing:
                raise ValueError(f"{path}: {self.label} requires {', '.join(missing)}")
        if self.label in {EpistemicLabel.SOURCE_INFERENCE, EpistemicLabel.AGENT_INFERENCE} and not self.rationale:
            raise ValueError(f"{path}: {self.label} requires rationale")
        if self.label is EpistemicLabel.USER_STATEMENT and not self.source_id:
            raise ValueError(f"{path}: user_statement requires source_id")
        representation = self.resolved_representation()
        if representation is SourceRepresentation.QUOTE and self.label in {
            EpistemicLabel.SOURCE_INFERENCE,
            EpistemicLabel.AGENT_INFERENCE,
            EpistemicLabel.HYPOTHESIS,
            EpistemicLabel.CREATIVE_SUGGESTION,
        }:
            raise ValueError(f"{path}.representation: {self.label} cannot be quoted source text")
        if representation is SourceRepresentation.QUOTE and self.label is EpistemicLabel.SOURCE_FACT and not self.span_hash:
            raise ValueError(f"{path}.span_hash: quoted source_fact requires exact span_hash")

    def as_dict(self) -> dict[str, str]:
        self.validate()
        data = {"label": self.label.value}
        for name in ("source_id", "content_hash", "span_hash", "rationale"):
            value = getattr(self, name)
            if value:
                data[name] = normalize(value)
        data["representation"] = self.resolved_representation().value
        data["claim_id"] = "cl_" + digest(data)[:16]
        return data


def claim_from_dict(data: dict[str, Any], path: str = "claim") -> Claim:
    try:
        claim = Claim(
            label=EpistemicLabel(data["label"]),
            source_id=data.get("source_id"),
            content_hash=data.get("content_hash"),
            span_hash=data.get("span_hash"),
            rationale=data.get("rationale"),
            representation=SourceRepresentation(data["representation"]) if data.get("representation") else None,
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{path}: invalid epistemic label") from exc
    claim.validate(path)
    return claim
