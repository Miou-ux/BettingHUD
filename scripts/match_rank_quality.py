"""Garde-fous rang/points pour Live Tracker, Paris du jour, API PREPROD."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

PARIS_TZ = ZoneInfo("Europe/Paris")

# Exclusion UI si stats_reference_date (TML / WTA) a plus de N jours vs date du match.
STALE_RANK_STATS_MAX_DAYS = max(
    30, int(os.getenv("BETTINGHUD_STALE_RANK_STATS_MAX_DAYS", "365"))
)


def _rank_stats_source_key(stats: dict | None) -> str | None:
    if not stats:
        return None
    s = str(stats.get("stats_source") or "").strip().lower()
    if not s or s == "no_ranking_source":
        return None
    return s


def stats_reference_age_days(
    stats: dict | None,
    *,
    anchor_date: str | None = None,
) -> int | None:
    """Âge en jours de ``stats_reference_date`` par rapport à la date du match (ou aujourd'hui)."""
    if not stats:
        return None
    ref = stats.get("stats_reference_date")
    if not ref:
        return None
    anchor = str(anchor_date or datetime.now(PARIS_TZ).strftime("%Y-%m-%d"))[:10]
    try:
        d_ref = pd.Timestamp(str(ref)[:10]).normalize()
        d_anchor = pd.Timestamp(anchor).normalize()
        return int(max(0, (d_anchor - d_ref).days))
    except Exception:
        return None


def player_rank_stats_fresh(
    stats: dict | None,
    *,
    anchor_date: str | None = None,
    max_days: int | None = None,
) -> bool:
    """True si la référence rang/points est présente et ≤ max_days (défaut 12 mois)."""
    limit = STALE_RANK_STATS_MAX_DAYS if max_days is None else max(1, int(max_days))
    age = stats_reference_age_days(stats, anchor_date=anchor_date)
    if age is None:
        return False
    return age <= limit


def match_rank_exclude_reason(
    match: dict,
    *,
    anchor_date: str | None = None,
    max_days: int | None = None,
) -> str | None:
    """Raison d'exclusion UI (None = éligible)."""
    if not isinstance(match, dict):
        return "invalid_match"
    p1 = match.get("p1_stats") or {}
    p2 = match.get("p2_stats") or {}
    k1 = _rank_stats_source_key(p1)
    k2 = _rank_stats_source_key(p2)
    if not k1 or not k2:
        return "missing_rank_source"
    if k1 == "tennisexplorer_estimate" or k2 == "tennisexplorer_estimate":
        return "tennisexplorer_estimate"
    anchor = anchor_date or str(match.get("date") or "")[:10] or None
    if not player_rank_stats_fresh(p1, anchor_date=anchor, max_days=max_days):
        return "stale_rank_stats"
    if not player_rank_stats_fresh(p2, anchor_date=anchor, max_days=max_days):
        return "stale_rank_stats"
    return None


def match_has_rank_points_source(match: dict) -> bool:
    """Même garde-fou que le Live Tracker / report algo / API PREPROD."""
    return match_rank_exclude_reason(match) is None


def count_matches_excluded_by_reason(
    matches: list[dict],
    *,
    anchor_date: str | None = None,
) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in matches:
        if not isinstance(m, dict):
            out["invalid_match"] = out.get("invalid_match", 0) + 1
            continue
        reason = match_rank_exclude_reason(m, anchor_date=anchor_date)
        if reason:
            out[reason] = out.get(reason, 0) + 1
    return out
