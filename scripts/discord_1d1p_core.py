"""Chargement du pick 1 Day 1 Pick (aligné CourtAlpha / one_day_one_pick)."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

PARIS_TZ = ZoneInfo("Europe/Paris")
EV_MIN_PCT = 15.0
EV_MAX_PCT = 100.0
PROBA_STANDARD_MIN_FRAC = 0.70
"""Legacy — conservé pour signatures API."""


def select_1d1p_pick(
    rows: list[dict[str, Any]],
    *,
    ev_min_pct: float = EV_MIN_PCT,
    ev_max_pct: float = EV_MAX_PCT,
    row_ok=None,
    proba_floor_frac: float = PROBA_STANDARD_MIN_FRAC,
    hybrid_limit: int | None = None,
) -> dict[str, Any] | None:
    """
    1D1P prod : meilleure proba dans HYB P75+P80-all (P75-TIER + P≥80 rel≥80).
    """
    _ = ev_min_pct, ev_max_pct, proba_floor_frac, hybrid_limit
    from scripts.hyb_p75_p80_selection import best_1d1p_pick_from_hyb, select_hyb_p75_p80_all
    from scripts.match_rank_quality import duplicate_model_prob_keys

    eligible = [r for r in rows if row_ok(r)] if row_ok is not None else list(rows)
    dup = duplicate_model_prob_keys(eligible)
    picks = select_hyb_p75_p80_all(eligible, duplicate_keys=dup, limit=None)
    pick = best_1d1p_pick_from_hyb(picks)
    if pick is not None:
        pick["source"] = pick.get("source") or "live"
    return pick


def load_1d1p_today_pick(
    *,
    db_path: str,
    calendar_date: str | None = None,
    ev_min_pct: float = EV_MIN_PCT,
    ev_max_pct: float = EV_MAX_PCT,
) -> tuple[dict[str, Any] | None, str, int, float | None]:
    """Retourne (pick, calendar_date, pool_size_hybride, snapshot_age_min)."""
    from scripts.daily_top_proba_store import (
        collect_hybrid_proba_picks,
        load_today_matches_for_daily_top_proba,
        paris_projection_date,
        snapshot_age_min_from_meta,
    )
    from scripts.hyb_p75_p80_selection import best_1d1p_pick_from_hyb, count_hyb_pool_candidates
    from scripts.match_rank_quality import duplicate_model_prob_keys
    from scripts.tournament_tier import is_major_atp_wta_by_name

    cal_day = calendar_date or paris_projection_date().isoformat()

    def _major_row(row: dict[str, Any]) -> bool:
        return is_major_atp_wta_by_name(
            str(row.get("tour") or ""),
            str(row.get("tournament") or ""),
        )

    matches, snap_meta = load_today_matches_for_daily_top_proba()
    snapshot_age_min = snapshot_age_min_from_meta(snap_meta)
    dup = duplicate_model_prob_keys(matches)
    pool_n = count_hyb_pool_candidates(
        [m for m in matches if _major_row(m)],
        duplicate_keys=dup,
    )

    hybrid = collect_hybrid_proba_picks(
        matches,
        limit=None,
        today_only=True,
        major_only=True,
        calendar_date=cal_day,
    )
    pick = best_1d1p_pick_from_hyb(hybrid)
    if pick is not None:
        pick = dict(pick)
        pick["source"] = "live"

    return pick, cal_day, pool_n, snapshot_age_min


def _courtalpha_root() -> str:
    raw = (os.getenv("COURTALPHA_ROOT") or "").strip()
    if raw and os.path.isdir(raw):
        return raw
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sibling = os.path.join(os.path.dirname(root), "CourtAlpha")
    if os.path.isdir(sibling):
        return sibling
    prod = "/opt/courtalpha"
    if os.path.isdir(prod):
        return prod
    return sibling


def load_1d1p_replay(*, db_path: str) -> dict[str, Any]:
    ca = _courtalpha_root()
    if ca not in sys.path:
        sys.path.insert(0, ca)
    from api.services.one_day_one_pick import build_one_day_one_pick_replay

    return build_one_day_one_pick_replay(db_path=db_path)
