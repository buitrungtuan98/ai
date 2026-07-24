"""Curated IANA timezone catalog for the campaign/channel dropdowns.

Free-text IANA entry was error-prone: a typo like ``Asia/HoChiMinh`` is silently dropped (profile)
or falls back to UTC at schedule time (campaign) — posts then fire at the wrong local hour with no
signal. This module is the single source of a friendly, grouped picker. Offsets are computed **per
render** (not at import) so they stay correct across DST transitions.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# (region, IANA name, friendly label). Ordered deliberately: Việt Nam + South-East Asia first (the
# primary operator market), then the wider Asia-Pacific, Oceania, Europe, the Americas, Africa /
# Middle East, and finally UTC. Add a row here to extend the picker — nothing else changes.
_ZONES: list[tuple[str, str, str]] = [
    ("Asia · Pacific", "Asia/Ho_Chi_Minh", "Việt Nam (Hà Nội · TP.HCM)"),
    ("Asia · Pacific", "Asia/Bangkok", "Thailand · Bangkok"),
    ("Asia · Pacific", "Asia/Jakarta", "Indonesia · Jakarta"),
    ("Asia · Pacific", "Asia/Singapore", "Singapore"),
    ("Asia · Pacific", "Asia/Kuala_Lumpur", "Malaysia · Kuala Lumpur"),
    ("Asia · Pacific", "Asia/Manila", "Philippines · Manila"),
    ("Asia · Pacific", "Asia/Yangon", "Myanmar · Yangon"),
    ("Asia · Pacific", "Asia/Phnom_Penh", "Cambodia · Phnom Penh"),
    ("Asia · Pacific", "Asia/Hong_Kong", "Hong Kong"),
    ("Asia · Pacific", "Asia/Shanghai", "China · Shanghai"),
    ("Asia · Pacific", "Asia/Taipei", "Taiwan · Taipei"),
    ("Asia · Pacific", "Asia/Seoul", "South Korea · Seoul"),
    ("Asia · Pacific", "Asia/Tokyo", "Japan · Tokyo"),
    ("Asia · Pacific", "Asia/Kolkata", "India · Kolkata"),
    ("Asia · Pacific", "Asia/Dubai", "UAE · Dubai"),
    ("Oceania", "Australia/Perth", "Australia · Perth"),
    ("Oceania", "Australia/Sydney", "Australia · Sydney"),
    ("Oceania", "Pacific/Auckland", "New Zealand · Auckland"),
    ("Europe", "Europe/London", "United Kingdom · London"),
    ("Europe", "Europe/Paris", "France · Paris"),
    ("Europe", "Europe/Berlin", "Germany · Berlin"),
    ("Europe", "Europe/Madrid", "Spain · Madrid"),
    ("Europe", "Europe/Rome", "Italy · Rome"),
    ("Europe", "Europe/Istanbul", "Türkiye · Istanbul"),
    ("Europe", "Europe/Moscow", "Russia · Moscow"),
    ("Americas", "America/New_York", "USA · New York (Eastern)"),
    ("Americas", "America/Chicago", "USA · Chicago (Central)"),
    ("Americas", "America/Denver", "USA · Denver (Mountain)"),
    ("Americas", "America/Los_Angeles", "USA · Los Angeles (Pacific)"),
    ("Americas", "America/Toronto", "Canada · Toronto"),
    ("Americas", "America/Mexico_City", "Mexico · Mexico City"),
    ("Americas", "America/Sao_Paulo", "Brazil · São Paulo"),
    ("Americas", "America/Argentina/Buenos_Aires", "Argentina · Buenos Aires"),
    ("Africa · Middle East", "Africa/Cairo", "Egypt · Cairo"),
    ("Africa · Middle East", "Africa/Lagos", "Nigeria · Lagos"),
    ("Africa · Middle East", "Africa/Nairobi", "Kenya · Nairobi"),
    ("Africa · Middle East", "Africa/Johannesburg", "South Africa · Johannesburg"),
    ("Universal", "UTC", "UTC — Coordinated Universal Time"),
]

# The set of zones the picker offers — used to tell a "known" value from a stored legacy/custom one.
KNOWN: frozenset[str] = frozenset(iana for _region, iana, _friendly in _ZONES)


def offset_label(tz_name: str) -> str:
    """Current UTC offset of a zone as ``UTC+07:00`` (DST-correct because it uses *now*). '' on error."""
    try:
        offset = datetime.now(ZoneInfo(tz_name)).utcoffset() or timedelta(0)
    except (ZoneInfoNotFoundError, ValueError):
        return ""
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def tz_choices() -> list[tuple[str, list[tuple[str, str]]]]:
    """Grouped picker options: ``[(region, [(iana, 'Friendly (UTC+07:00)'), …]), …]``, region order
    preserved from ``_ZONES``. Computed per call so offsets track DST."""
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    for region, iana, friendly in _ZONES:
        label = f"{friendly} · {offset_label(iana)}"
        if not groups or groups[-1][0] != region:
            groups.append((region, []))
        groups[-1][1].append((iana, label))
    return groups


def is_valid(tz_name: str) -> bool:
    """True if `tz_name` is a real IANA zone (any zone, not just the curated picker set)."""
    try:
        ZoneInfo(tz_name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False
