"""Formatage dates/heures Europe/Paris (CET/CEST) pour l'UI."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")


def _parse_calendar_date(raw: object) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if " " in s:
        s = s.split()[0]
    if len(s) >= 10 and s[4:5] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=PARIS)
        except ValueError:
            return None
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").replace(tzinfo=PARIS)
        except ValueError:
            return None
    return None


def format_date_eu(raw: object) -> str:
    """Date calendrier DD/MM/YYYY (Paris)."""
    dt = _parse_calendar_date(raw)
    if dt is None:
        s = str(raw or "").strip()
        return s if s else "—"
    return dt.strftime("%d/%m/%Y")


def format_datetime_cet(raw: object) -> str:
    """Horodatage DD/MM/YYYY HH:MM (fuseau Europe/Paris)."""
    if raw is None:
        return "— (jamais enregistré)"
    if isinstance(raw, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(raw), tz=timezone.utc).astimezone(PARIS)
            return dt.strftime("%d/%m/%Y %H:%M") + " (CET)"
        except (OSError, OverflowError, ValueError, TypeError):
            return "—"
    s = str(raw).strip()
    if not s:
        return "— (jamais enregistré)"
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PARIS).strftime("%d/%m/%Y %H:%M") + " (CET)"
    except ValueError:
        return s
