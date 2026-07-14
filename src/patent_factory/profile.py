from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .provenance import Claim, EpistemicLabel, canonical_json, claim_from_dict, digest, normalize, strict_json_loads

PROFILE_VERSION = "profile-v1"
ALLOWED_SUFFIXES = {".json", ".md", ".txt"}
MAX_DOCUMENT_BYTES = 2_000_000


@dataclass(frozen=True)
class IncomingFact:
    field: str
    value: Any
    claim: Claim

    def normalized(self) -> "IncomingFact":
        field = normalize(self.field)
        if not field or not isinstance(field, str):
            raise ValueError("fact.field: non-empty string required")
        if self.value is None or normalize(self.value) == "":
            raise ValueError(f"fact.{field}.value: non-empty value required")
        self.claim.validate(f"fact.{field}.claim")
        return IncomingFact(field, normalize(self.value), self.claim)


def empty_profile() -> dict[str, Any]:
    return {"facts": {}, "profile_version": PROFILE_VERSION}


def load_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_profile()
    data = strict_json_loads(path.read_text(encoding="utf-8"))
    if data.get("profile_version") != PROFILE_VERSION or not isinstance(data.get("facts"), dict):
        raise ValueError("profile: unsupported or malformed profile")
    return data


def merge_profile(profile: dict[str, Any], incoming: Iterable[IncomingFact]) -> tuple[dict[str, Any], list[dict[str, str]], int]:
    batch = [fact.normalized() for fact in incoming]
    proposed: dict[str, Any] = json.loads(canonical_json(profile))
    conflicts: list[dict[str, str]] = []
    pending_values: dict[str, Any] = {}
    for fact in batch:
        existing = profile["facts"].get(fact.field)
        prior = pending_values.get(fact.field, existing["value"] if existing else fact.value)
        if canonical_json(prior) != canonical_json(fact.value):
            conflicts.append({
                "existing_value_hash": digest(prior),
                "field": fact.field,
                "incoming_value_hash": digest(fact.value),
                "incoming_source_id": fact.claim.source_id or "",
            })
        pending_values[fact.field] = prior
    if conflicts:
        return profile, sorted(conflicts, key=lambda item: (item["field"], item["incoming_source_id"])), 0

    changes = 0
    for fact in batch:
        claim = fact.claim.as_dict()
        entry = proposed["facts"].get(fact.field)
        if entry is None:
            proposed["facts"][fact.field] = {"claims": [claim], "value": fact.value}
            changes += 1
        elif all(item["claim_id"] != claim["claim_id"] for item in entry["claims"]):
            entry["claims"].append(claim)
            entry["claims"].sort(key=lambda item: item["claim_id"])
            changes += 1
    return proposed, [], changes


def atomic_write_profile(path: Path, profile: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    payload = json.dumps(profile, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    handle, temporary = tempfile.mkstemp(prefix=".profile-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _safe_document(path: Path, root: Path | None = None) -> tuple[Path, str]:
    if path.is_symlink():
        raise ValueError(f"input rejected: symbolic link: {path.name}")
    resolved = path.resolve(strict=True)
    if root is None:
        root_resolved = resolved.parent
    else:
        root_resolved = root.resolve(strict=True)
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError(f"input rejected: document outside root: {path}") from exc
    if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode) or resolved.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError(f"input rejected: unsupported document: {path.name}")
    if resolved.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError(f"input rejected: document too large: {path.name}")
    return resolved, resolved.relative_to(root_resolved).as_posix()


def document_facts(path: Path, root: Path | None = None) -> list[IncomingFact]:
    resolved, source_locator = _safe_document(path, root)
    raw = resolved.read_bytes()
    text = raw.decode("utf-8")
    content_hash = digest(text)
    source_id = "src_" + digest({"locator": source_locator, "content_hash": content_hash})[:16]
    if resolved.suffix.lower() == ".json":
        data = strict_json_loads(text)
        if isinstance(data, dict) and isinstance(data.get("facts"), list):
            facts = []
            for index, item in enumerate(data["facts"]):
                if not isinstance(item, dict):
                    raise ValueError(f"facts[{index}]: object required")
                claim_data = item.get("claim") or {"label": "source_fact", "source_id": source_id, "content_hash": content_hash, "span_hash": digest(item)}
                facts.append(IncomingFact(str(item["field"]), item["value"], claim_from_dict(claim_data, f"facts[{index}].claim")))
            return facts
        if not isinstance(data, dict):
            raise ValueError("document: JSON object required")
        pairs = [(str(key), value, canonical_json({key: value})) for key, value in sorted(data.items())]
    else:
        pairs = []
        for line_number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            field, value = stripped.split(":", 1)
            pairs.append((field, value, f"{line_number}:{stripped}"))
    return [IncomingFact(field, value, Claim(EpistemicLabel.SOURCE_FACT, source_id, content_hash, digest(span))) for field, value, span in pairs]


def folder_facts(path: Path) -> list[IncomingFact]:
    if path.is_symlink():
        raise ValueError("input rejected: folder required")
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("input rejected: folder required")

    facts: list[IncomingFact] = []

    def collect(folder: Path) -> None:
        for item in sorted(folder.iterdir(), key=lambda child: child.name):
            if item.is_symlink():
                raise ValueError(f"input rejected: symbolic link: {item.relative_to(root)}")
            mode = item.stat(follow_symlinks=False).st_mode
            resolved = item.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"input rejected: entry outside root: {item}") from exc
            if stat.S_ISDIR(mode):
                collect(item)
            elif not stat.S_ISREG(mode):
                raise ValueError(f"input rejected: nonregular path: {item.relative_to(root)}")
            elif item.suffix.lower() in ALLOWED_SUFFIXES:
                facts.extend(document_facts(item, root=root))

    collect(root)
    return facts


def interview_facts(responses: dict[str, Any]) -> list[IncomingFact]:
    facts = []
    for field, value in sorted(responses.items()):
        if value is None or normalize(value) == "":
            continue
        facts.append(IncomingFact(str(field), value, Claim(EpistemicLabel.USER_STATEMENT, "interview-v1")))
    return facts
