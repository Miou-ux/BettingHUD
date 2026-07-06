"""Contexte fiabilité extrait du snapshot — utilisable hors Streamlit."""
from __future__ import annotations

from scripts.match_rank_quality import (
    STALE_RANK_STATS_MAX_DAYS,
    _rank_stats_source_key,
    player_rank_stats_fresh,
    stats_reference_age_days,
)

TE_INACTIVITY_BRIDGE_DAYS = 60
STALE_REF_DATE_DAYS = 30
STALE_PLAYER_DATA_DAYS = 60

_OFFICIAL_RANK_SOURCES = frozenset(
    {"matches_recent", "wta_matches", "rankings_wta_current"}
)


def _player_stats(match: dict, player_side: int) -> dict:
    return (match.get("p1_stats") if int(player_side) == 1 else match.get("p2_stats")) or {}


def _player_circuit(match: dict, player_side: int) -> str:
    tour = str(match.get("tour") or "").upper()
    if tour in ("ATP", "WTA"):
        return tour
    st = _player_stats(match, player_side)
    src = str(st.get("stats_source") or "").lower()
    if "wta" in src:
        return "WTA"
    return "ATP"


def player_hist_te_data_conflict(match: dict, player_side: int) -> dict | None:
    """Conflit base officielle (longue absence) vs activité récente TE (blend inactivité)."""
    fs = match.get("feature_snapshot") or {}
    side = int(player_side)
    if not fs.get(f"p{side}_te_inactivity_blend_used"):
        return None
    try:
        pre_d = float(fs.get(f"p{side}_days_inactivity_pre_te") or 0)
        eff_d = float(fs.get(f"p{side}_days_since_last_match") or 0)
    except (TypeError, ValueError):
        return None
    if pre_d < float(TE_INACTIVITY_BRIDGE_DAYS) or eff_d + 1e-6 >= pre_d:
        return None
    circuit = _player_circuit(match, side)
    return {"pre_days": int(pre_d), "eff_days": int(eff_d), "circuit": circuit}


def match_has_hist_te_conflict(match: dict, circuit: str | None = None) -> bool:
    want = str(circuit or "").strip().upper() or None
    for side in (1, 2):
        info = player_hist_te_data_conflict(match, side)
        if info is None:
            continue
        if want is None:
            return True
        if str(info.get("circuit") or "").upper() == want:
            return True
    return False


def player_ref_date_stale(match: dict, player_side: int) -> bool:
    st = _player_stats(match, player_side)
    age = stats_reference_age_days(st, anchor_date=str(match.get("date") or "")[:10] or None)
    return age is not None and age > STALE_REF_DATE_DAYS


def player_data_stale(match: dict, player_side: int) -> bool:
    fs = match.get("feature_snapshot") or {}
    side = int(player_side)
    pre_key = f"p{side}_days_inactivity_pre_te"
    pre_raw = fs.get(pre_key)
    if pre_raw is not None:
        try:
            return float(pre_raw) > float(STALE_PLAYER_DATA_DAYS)
        except (TypeError, ValueError):
            pass
    raw = fs.get(f"p{side}_days_since_last_match")
    if raw is None:
        return False
    try:
        return float(raw) > float(STALE_PLAYER_DATA_DAYS)
    except (TypeError, ValueError):
        return False


def match_has_official_fresh_ranks(match: dict) -> bool:
    """Les deux joueurs ont rang/points officiels et référence ≤ 12 mois."""
    anchor = str(match.get("date") or "")[:10] or None
    for side in (1, 2):
        st = _player_stats(match, side)
        src = _rank_stats_source_key(st)
        if src not in _OFFICIAL_RANK_SOURCES:
            return False
        if not player_rank_stats_fresh(st, anchor_date=anchor, max_days=STALE_RANK_STATS_MAX_DAYS):
            return False
    return True
