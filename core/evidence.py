"""Canonical, browser-independent evidence snapshot validation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

MAX_URL_CHARS = 4096
MAX_CONTENT_CHARS = 500_000
MAX_SECTIONS = 500
MAX_SECTION_CHARS = 20_000


@dataclass(frozen=True)
class EvidenceSnapshot:
    source_kind: str
    title: str
    source_url: str
    canonical_url: str | None
    captured_at: str
    content_sha256: str
    content: str
    sections: tuple[str, ...]

    @property
    def provenance(self) -> dict[str, str]:
        return {
            "source_kind": self.source_kind,
            "provenance_basis": "client_asserted",
            "source_url": self.source_url,
            "canonical_url": self.canonical_url or "",
            "captured_at": self.captured_at,
            "content_sha256": self.content_sha256,
        }


def build_browser_snapshot(payload: Any) -> EvidenceSnapshot:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    title = _required_text(payload.get("title"), "title", MAX_SECTION_CHARS)
    source_url = _http_url(payload.get("source_url"), "source_url", required=True)
    canonical_url = _http_url(payload.get("canonical_url"), "canonical_url", required=False)
    content = _required_text(payload.get("content"), "content", MAX_CONTENT_CHARS)
    sections = _sections(payload.get("sections"), content)
    captured_at = _captured_at(payload.get("captured_at"))
    normalized = "\n\n".join(sections)
    return EvidenceSnapshot(
        source_kind="browser_current_page",
        title=title,
        source_url=source_url,
        canonical_url=canonical_url,
        captured_at=captured_at,
        content_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        content=normalized,
        sections=tuple(sections),
    )


def _required_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{name} must be a non-empty string")
    if len(text) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return text


def _http_url(value: Any, name: str, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an http(s) URL")
    url = value.strip()
    if not url or len(url) > MAX_URL_CHARS:
        raise ValueError(f"{name} must be an http(s) URL")
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname.isascii()
    ):
        raise ValueError(f"{name} must be an http(s) URL")
    return url


def _sections(value: Any, content: str) -> list[str]:
    if value is None:
        raw = content.split("\n\n")
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw = value
    else:
        raise ValueError("sections must be a string array")
    sections = [_normalize_section(item) for item in raw]
    sections = [item for item in sections if item]
    if not sections:
        raise ValueError("sections must contain at least one non-empty string")
    if len(sections) > MAX_SECTIONS:
        raise ValueError(f"sections exceeds {MAX_SECTIONS} entries")
    if any(len(item) > MAX_SECTION_CHARS for item in sections):
        raise ValueError(f"a section exceeds {MAX_SECTION_CHARS} characters")
    return sections


def _normalize_section(value: str) -> str:
    """Normalize only line endings and edge whitespace; preserve order/content."""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _captured_at(value: Any) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    if not isinstance(value, str):
        raise ValueError("captured_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("captured_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("captured_at must include a timezone")
    return parsed.astimezone(UTC).isoformat()
