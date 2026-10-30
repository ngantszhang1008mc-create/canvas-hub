"""Date evidence handling.

A collected date is stored as a dict, never as a bare string:

    {
      "raw": "2026-10-30T15:59:00Z" | "Oct 30 at 11:59pm",   # exactly as the source gave it
      "iso": "2026-10-30T15:59:00Z" | null,                 # only when instant is established
      "source": "canvas_api_iso8601" | "display_text",
      "precision": "instant" | "date_only" | "unresolved",
      "tz_evidence": "offset_in_source" | "account_time_zone:Asia/Hong_Kong" | null,
      "note": "...",                                       # why iso is null, if it is
    }

A field value of ``None`` means the source explicitly showed no date
(e.g. Canvas API ``due_at: null`` = no due date). A field that is absent from
an observation means it was not collected - the two are never conflated.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .util import parse_iso, to_utc_iso

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}


def from_api(value) -> dict | None:
    """Canvas REST API timestamps are ISO-8601 with an explicit offset/Z."""
    if value is None:
        return None
    raw = str(value)
    dt = parse_iso(raw)
    if dt is None:
        return {"raw": raw, "iso": None, "source": "canvas_api_iso8601", "precision": "unresolved",
                "tz_evidence": None, "note": "unparseable timestamp"}
    return {"raw": raw, "iso": to_utc_iso(dt), "source": "canvas_api_iso8601", "precision": "instant",
            "tz_evidence": "offset_in_source"}


_TEXT_RE = re.compile(
    r"\b(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?!\d)(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>\d{4})(?!\d))?"
    r"(?:\s*(?:at|,)?\s*(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?))?",
    re.IGNORECASE,
)
_TEXT_RE_DMY = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon>[A-Za-z]{3,9})\.?(?:,?\s+(?P<year>\d{4})(?!\d))?"
    r"(?:\s*(?:at|,)?\s*(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?))?",
    re.IGNORECASE,
)


def from_display_text(text: str | None, *, time_zone: str | None = None, year: int | None = None) -> dict | None:
    """Interpret date text shown in the Canvas UI.

    Only produces an instant when the year is in the text (or explicitly
    supplied from trustworthy context) AND the time zone is known. Relative
    phrases ("2 days ago") are kept raw.
    """
    if text is None:
        return None
    raw = " ".join(str(text).split())
    out = {"raw": raw, "iso": None, "source": "display_text", "precision": "unresolved", "tz_evidence": None}
    cands = [m for rx in (_TEXT_RE, _TEXT_RE_DMY) for m in rx.finditer(raw)
             if m.group("mon").lower().rstrip(".") in _MONTHS]
    if not cands:
        out["note"] = "no absolute date recognised"
        return out
    m = min(cands, key=lambda x: (x.start(), -len(x.group(0))))
    mon = _MONTHS[m.group("mon").lower().rstrip(".")]
    day = int(m.group("day"))
    yr = int(m.group("year")) if m.group("year") else year
    if yr is None:
        out["note"] = "year not shown; not inferred"
        out["month_day"] = f"{mon:02d}-{day:02d}"
        return out
    try:
        d = date(yr, mon, day)
    except ValueError:
        out["note"] = "invalid calendar date"
        return out
    if not m.group("h"):
        out.update(precision="date_only", date=d.isoformat())
        return out
    if not time_zone:
        out["note"] = "time zone not established"
        out["date"] = d.isoformat()
        return out
    try:
        tz = ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        out["note"] = f"unknown time zone {time_zone!r}"
        return out
    h = int(m.group("h")) % 12
    if m.group("ampm").lower().startswith("p"):
        h += 12
    mi = int(m.group("mi") or 0)
    dt = datetime(d.year, d.month, d.day, h, mi, tzinfo=tz)
    out.update(iso=to_utc_iso(dt), precision="instant", tz_evidence=f"account_time_zone:{time_zone}")
    return out


def instant(value) -> datetime | None:
    if isinstance(value, dict) and value.get("iso"):
        return parse_iso(value["iso"])
    return None


def compare_key(value) -> str | None:
    """What is compared for change detection: the instant when established,
    otherwise the raw wording."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("iso") or ("raw:" + str(value.get("raw")))
    return str(value)


def format_local(value, time_zone: str | None) -> str | None:
    dt = instant(value)
    if dt is None:
        if isinstance(value, dict):
            return value.get("raw")
        return None
    if time_zone:
        try:
            dt = dt.astimezone(ZoneInfo(time_zone))
        except (ZoneInfoNotFoundError, ValueError):
            dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%a %d %b %Y %H:%M %Z")


def date_mentions(text: str) -> list[dict]:
    """Find month/day mentions in free text (used to flag announcement vs
    assignment date discrepancies). Purely numeric dates are ignored since
    their D/M vs M/D order is ambiguous."""
    found = []
    for rx in (_TEXT_RE, _TEXT_RE_DMY):
        for m in rx.finditer(text or ""):
            mon_name = m.group("mon").lower().rstrip(".")
            if mon_name not in _MONTHS:
                continue
            day = int(m.group("day"))
            if not 1 <= day <= 31:
                continue
            found.append({
                "month": _MONTHS[mon_name], "day": day,
                "year": int(m.group("year")) if m.group("year") else None,
                "text": m.group(0).strip().rstrip(".,;:"),
            })
    seen, uniq = set(), []
    for f in found:
        k = (f["month"], f["day"], f["text"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq
