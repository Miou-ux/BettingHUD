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

MIN_DATA_RELIABILITY_SCORE = max(
    0, min(100, int(os.getenv("BETTINGHUD_MIN_DATA_RELIABILITY", "80")))
)

# Seuil doc / flag ``book_gap_high`` (pénalité score fiabilité — pas filtre de sélection).
BOOK_GAP_HIGH_PP = float(os.getenv("BETTINGHUD_BOOK_GAP_HIGH_PP", "25"))


def book_gap_pp_from_favorite(p_model_fav: object, odd_fav: object) -> float | None:
    """Écart |p_modèle(favori) − p_implicite(book)| en points de pourcentage.

    Règle unique prod : cote **publique** du favori modèle (pas ``true_odd`` / marge).
    """
    try:
        p = float(p_model_fav)
        o = float(odd_fav)
    except (TypeError, ValueError):
        return None
    if o <= 1.0 or not (0.0 <= p <= 1.0):
        return None
    return abs(p - (1.0 / o)) * 100.0


def book_gap_pp_from_match(match: dict) -> float | None:
    """``book_gap_pp`` canonique depuis une ligne snapshot (``feature_snapshot`` + cotes)."""
    if not isinstance(match, dict):
        return None
    fs = match.get("feature_snapshot") or {}
    try:
        p1 = float(fs.get("capped_p1_prob") or 0.5)
        odd_p1 = float(match.get("odd_p1") or 0.0)
        odd_p2 = float(match.get("odd_p2") or 0.0)
    except (TypeError, ValueError):
        return None
    if odd_p1 <= 1.0 or odd_p2 <= 1.0:
        return None
    fav_side = 1 if p1 >= 0.5 else 2
    fav_p = max(p1, 1.0 - p1)
    odd_fav = odd_p1 if fav_side == 1 else odd_p2
    return book_gap_pp_from_favorite(fav_p, odd_fav)


def attach_book_gap_pp(match: dict) -> dict:
    """Attache ``book_gap_pp`` si absent (idempotent)."""
    if not isinstance(match, dict):
        return match
    if match.get("book_gap_pp") is not None:
        return match
    gap = book_gap_pp_from_match(match)
    if gap is not None:
        match["book_gap_pp"] = gap
    return match


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


def _player_rank_placeholder(stats: dict | None) -> bool:
    if not stats:
        return False
    try:
        rank = int(stats.get("rank") or 0)
        pts = float(stats.get("pts") or 0)
        return rank >= 1500 or pts < 10.0
    except (TypeError, ValueError):
        return True


def match_data_reliability_score(
    match: dict,
    *,
    hist_te_conflict: bool = False,
    ref_date_stale_sides: tuple[bool, bool] = (False, False),
    data_stale_sides: tuple[bool, bool] = (False, False),
) -> tuple[int, list[str]]:
    """Score 0–100 de confiance dans les données d'une ligne snapshot (hors qualité tennis pure).

    100 = identités + rangs récents + proba cohérente ; pénalités cumulatives documentées dans
  `docs/DATA_RELIABILITY.md`.
    """
    if not isinstance(match, dict):
        return 0, ["invalid_match"]
    score = 100
    flags: list[str] = []
    anchor = str(match.get("date") or "")[:10] or None

    if match.get("unreliable"):
        score -= 40
        flags.append("rang_vs_proba")

    if hist_te_conflict:
        score -= 20
        flags.append("hist_te_conflict")

    for side, pk, pid_key, ref_stale, data_stale in (
        ("p1", "p1_stats", "p1_player_id", ref_date_stale_sides[0], data_stale_sides[0]),
        ("p2", "p2_stats", "p2_player_id", ref_date_stale_sides[1], data_stale_sides[1]),
    ):
        st = match.get(pk) or {}
        if not match.get(pid_key):
            score -= 20
            flags.append(f"{side}_unresolved_id")
        src = _rank_stats_source_key(st)
        if not src:
            score -= 12
            flags.append(f"{side}_no_rank_source")
        elif src == "tennisexplorer_estimate":
            score -= 15
            flags.append(f"{side}_te_estimate")
        if _player_rank_placeholder(st):
            score -= 20
            flags.append(f"{side}_rank_placeholder")
        if not player_rank_stats_fresh(st, anchor_date=anchor):
            score -= 10
            flags.append(f"{side}_stale_rank_ref")
        if ref_stale:
            score -= 8
            flags.append(f"{side}_ref_date_stale")
        elif data_stale:
            score -= 6
            flags.append(f"{side}_data_stale")

    if str(match.get("snapshot_tier") or "full") == "preview":
        score -= 15
        flags.append("preview_tier")

    try:
        gap = float(match.get("book_gap_pp")) if match.get("book_gap_pp") is not None else None
    except (TypeError, ValueError):
        gap = None
    if gap is not None and gap > BOOK_GAP_HIGH_PP:
        score -= min(20, int((gap - BOOK_GAP_HIGH_PP) / 3.0))
        flags.append("book_gap_high")

    score = max(0, min(100, score))
    return score, flags


def ensure_match_reliability_scored(match: dict) -> dict:
    """Calcule et attache le score si absent (snapshot / replay / backfill)."""
    if not isinstance(match, dict):
        return match
    attach_book_gap_pp(match)
    if match.get("data_reliability_score") is not None:
        return match
    score, flags = match_data_reliability_score(match)
    match["data_reliability_score"] = score
    match["data_reliability_flags"] = "|".join(flags) if flags else None
    return match


def reliability_fields_from_match(match: dict | None) -> dict[str, object]:
    """Champs à persister depuis une ligne snapshot."""
    if not isinstance(match, dict):
        return {"data_reliability_score": None, "data_reliability_flags": None}
    flags = match.get("data_reliability_flags")
    if isinstance(flags, list):
        flags_s = "|".join(str(x) for x in flags if x) or None
    else:
        flags_s = str(flags).strip() if flags else None
    score = match.get("data_reliability_score")
    try:
        score_i = int(score) if score is not None else None
    except (TypeError, ValueError):
        score_i = None
    return {"data_reliability_score": score_i, "data_reliability_flags": flags_s}


def passes_data_reliability_filter(
    match_or_pick: dict | None,
    *,
    min_score: int | None = None,
) -> bool:
    """True si la ligne est proposable (pas ``unreliable``, score >= seuil)."""
    if not isinstance(match_or_pick, dict):
        return False
    if match_or_pick.get("unreliable"):
        return False
    threshold = MIN_DATA_RELIABILITY_SCORE if min_score is None else int(min_score)
    score = match_or_pick.get("data_reliability_score")
    if score is None:
        return False
    try:
        return int(score) >= threshold
    except (TypeError, ValueError):
        return False


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
