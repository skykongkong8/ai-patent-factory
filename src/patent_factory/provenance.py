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


def normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.strip().replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize(value[key]) for key in sorted(value)}
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Claim:
    label: EpistemicLabel
    source_id: str | None = None
    content_hash: str | None = None
    span_hash: str | None = None
    rationale: str | None = None

    def validate(self, path: str = "claim") -> None:
        if self.label in {EpistemicLabel.SOURCE_FACT, EpistemicLabel.SOURCE_INFERENCE}:
            missing = [name for name in ("source_id", "content_hash", "span_hash") if not getattr(self, name)]
            if missing:
                raise ValueError(f"{path}: {self.label} requires {', '.join(missing)}")
        if self.label in {EpistemicLabel.SOURCE_INFERENCE, EpistemicLabel.AGENT_INFERENCE} and not self.rationale:
            raise ValueError(f"{path}: {self.label} requires rationale")
        if self.label is EpistemicLabel.USER_STATEMENT and not self.source_id:
            raise ValueError(f"{path}: user_statement requires source_id")

    def as_dict(self) -> dict[str, str]:
        self.validate()
        data = {"label": self.label.value}
        for name in ("source_id", "content_hash", "span_hash", "rationale"):
            value = getattr(self, name)
            if value:
                data[name] = normalize(value)
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
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{path}: invalid epistemic label") from exc
    claim.validate(path)
    return claim
