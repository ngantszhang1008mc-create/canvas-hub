"""Small shared helpers: time, hashing, JSON, opaque cursors."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any


class HubError(Exception):
    """Base class for errors surfaced through the service boundary.

    ``code`` is a stable machine-readable identifier documented in
    CODEX_HANDOFF.md; ``status`` is the HTTP status used by the local API.
    """

    code = "internal_error"
    status = 500

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None, detail: Any = None):
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.status = status
        self.detail = detail

    def to_dict(self) -> dict:
        d = {"error": self.code, "message": str(self)}
        if self.detail is not None:
            d["detail"] = self.detail
        return d


class NotFound(HubError):
    code = "not_found"
    status = 404


class InvalidInput(HubError):
    code = "invalid_input"
    status = 400


class Conflict(HubError):
    code = "conflict"
    status = 409


class IdentityMismatch(HubError):
    code = "identity_mismatch"
    status = 409


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_cursor(payload: dict) -> str:
    raw = canonical_json(payload).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict | None:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + pad))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception as e:  # noqa: BLE001
        raise InvalidInput("Malformed cursor", code="invalid_cursor") from e


def clamp_limit(limit: int | None, default: int = 25, maximum: int = 100) -> int:
    if limit is None:
        return default
    try:
        n = int(limit)
    except (TypeError, ValueError) as e:
        raise InvalidInput("limit must be an integer") from e
    if n < 1:
        raise InvalidInput("limit must be >= 1")
    return min(n, maximum)
