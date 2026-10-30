"""Observation package format (version 1).

An observation package is the *only* way Canvas-derived data enters the
store - the built-in browser collector produces packages and imports them
through the same validated importer as an external trusted collector would.
See CODEX_HANDOFF.md section 5 and tests/fixtures/observation_package_v1.json.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import OBSERVATION_FORMAT, OBSERVATION_FORMAT_VERSION
from .model import CATEGORIES, COVERAGE_STATUSES, DEPTHS, ITEM_TYPES
from .urls import parse_canvas_address
from .util import parse_iso

_ID_CHARS = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_.~:")


def _check_ts(v: Optional[str], name: str) -> Optional[str]:
    if v is None:
        return v
    if parse_iso(v) is None:
        raise ValueError(f"{name} must be an ISO-8601 timestamp with a time zone offset")
    return v


def _check_id(v: Optional[str], name: str) -> Optional[str]:
    if v is None:
        return v
    v = str(v)
    if not v or len(v) > 200 or not set(v) <= _ID_CHARS:
        raise ValueError(f"{name} contains unsupported characters")
    return v


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(Strict):
    origin: str
    canvas_user_id: str
    display_name: Optional[str] = None
    login_id: Optional[str] = None
    primary_email: Optional[str] = None
    time_zone: Optional[str] = None
    time_zone_source: Optional[str] = None
    locale: Optional[str] = None

    @field_validator("origin")
    @classmethod
    def _origin(cls, v: str) -> str:
        addr = parse_canvas_address(v)
        if addr.origin != v.rstrip("/"):
            raise ValueError("origin must be scheme://host[:port] with no path")
        return addr.origin

    @field_validator("canvas_user_id")
    @classmethod
    def _uid(cls, v: str) -> str:
        return _check_id(v, "canvas_user_id")


class Collector(Strict):
    name: str
    version: str
    method: str


class Run(Strict):
    mode: Literal["baseline", "thorough", "quick", "import"]
    scope: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def _ts(cls, v, info):
        return _check_ts(v, info.field_name)


class ItemRef(Strict):
    type: str
    course_id: Optional[str] = None
    canvas_id: Optional[str] = None
    source_key: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None

    @field_validator("type")
    @classmethod
    def _type(cls, v):
        if v not in ITEM_TYPES:
            raise ValueError(f"unknown item type {v!r}")
        return v

    @field_validator("course_id", "canvas_id")
    @classmethod
    def _ids(cls, v, info):
        return _check_id(v, info.field_name)

    @model_validator(mode="after")
    def _has_identity(self):
        if not (self.canvas_id or self.source_key or self.url):
            raise ValueError("item reference needs canvas_id, source_key or url")
        return self


class LinkObs(Strict):
    relation: str
    to: ItemRef
    label: str = ""
    position: Optional[int] = None
    attrs: dict[str, Any] = Field(default_factory=dict)


class ResourceObs(Strict):
    sha256: str
    filename: Optional[str] = None

    @field_validator("sha256")
    @classmethod
    def _sha(cls, v):
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return v


class ItemObs(ItemRef):
    observed_at: str
    observation_id: Optional[str] = None
    method: str = "unspecified"
    depth: Literal["index", "body", "detail"] = "index"
    source_updated_at: Optional[str] = None
    fields: dict[str, Any] = Field(default_factory=dict)
    links: list[LinkObs] = Field(default_factory=list)
    resources: list[ResourceObs] = Field(default_factory=list)
    replace_links: list[str] = Field(default_factory=list)
    """Relations whose full set is given here; older links with these relations
    that are not repeated are retired (e.g. module membership)."""

    @field_validator("observed_at", "source_updated_at")
    @classmethod
    def _ts(cls, v, info):
        return _check_ts(v, info.field_name)

    @field_validator("fields")
    @classmethod
    def _fields(cls, v):
        for k in v:
            if not k or len(k) > 64 or not k.replace("_", "").isalnum():
                raise ValueError(f"invalid field name {k!r}")
        return v


class AbsenceInference(Strict):
    item_types: list[str]

    @field_validator("item_types")
    @classmethod
    def _types(cls, v):
        for t in v:
            if t not in ITEM_TYPES:
                raise ValueError(f"unknown item type {t!r}")
        return v


class CoverageObs(Strict):
    course_id: Optional[str] = None
    category: str
    status: str
    index_depth: str = "not_checked"
    body_depth: str = "not_checked"
    attachment_depth: str = "not_checked"
    expected_count: Optional[int] = None
    collected_count: Optional[int] = None
    reason: Optional[str] = None
    detail: dict[str, Any] = Field(default_factory=dict)
    observed_at: str
    absence_inference: Optional[AbsenceInference] = None
    """Only set when the index pass for these item types was exhaustive and
    comparable, which permits marking unseen items 'not in latest index'."""

    @field_validator("category")
    @classmethod
    def _cat(cls, v):
        if v not in CATEGORIES:
            raise ValueError(f"unknown category {v!r}")
        return v

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v not in COVERAGE_STATUSES:
            raise ValueError(f"unknown coverage status {v!r}")
        return v

    @field_validator("index_depth", "body_depth", "attachment_depth")
    @classmethod
    def _depth(cls, v):
        if v not in DEPTHS:
            raise ValueError(f"unknown depth {v!r}")
        return v

    @field_validator("observed_at")
    @classmethod
    def _ts(cls, v, info):
        return _check_ts(v, info.field_name)


class PresenceCheck(Strict):
    item: ItemRef
    result: Literal["available", "not_found", "forbidden", "unknown"]
    http_status: Optional[int] = None
    checked_at: str

    @field_validator("checked_at")
    @classmethod
    def _ts(cls, v, info):
        return _check_ts(v, info.field_name)


class ObservationPackage(Strict):
    format: Literal["canvas-hub.observations"]
    format_version: int
    package_id: str
    generated_at: str
    collector: Collector
    identity: Identity
    run: Run
    items: list[ItemObs] = Field(default_factory=list)
    coverage: list[CoverageObs] = Field(default_factory=list)
    presence_checks: list[PresenceCheck] = Field(default_factory=list)

    @field_validator("format_version")
    @classmethod
    def _ver(cls, v):
        if v != OBSERVATION_FORMAT_VERSION:
            raise ValueError(f"unsupported format_version {v}; this app accepts {OBSERVATION_FORMAT_VERSION}")
        return v

    @field_validator("package_id")
    @classmethod
    def _pid(cls, v):
        return _check_id(v, "package_id")

    @field_validator("generated_at")
    @classmethod
    def _ts(cls, v, info):
        return _check_ts(v, info.field_name)


def new_package(*, package_id: str, generated_at: str, identity: dict, mode: str, scope: dict,
                collector_version: str, method: str = "playwright_browser_session") -> dict:
    return {
        "format": OBSERVATION_FORMAT,
        "format_version": OBSERVATION_FORMAT_VERSION,
        "package_id": package_id,
        "generated_at": generated_at,
        "collector": {"name": "canvas-hub-browser-collector", "version": collector_version, "method": method},
        "identity": identity,
        "run": {"mode": mode, "scope": scope},
        "items": [],
        "coverage": [],
        "presence_checks": [],
    }


def validate_package(data: Any) -> ObservationPackage:
    return ObservationPackage.model_validate(data)
