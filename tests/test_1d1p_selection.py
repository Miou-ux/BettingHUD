"""Tests sélection 1 Day 1 Pick — fall-through EV par circuit."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.daily_top_proba_store import collect_daily_top_proba_rows
from scripts.discord_1d1p_core import (
    _best_circuit_candidate_between_circuits,
    _first_ev_eligible_per_circuit,
    _passes_ev_band,
)


def _match(
    p1,
    p2,
    p1_prob,
    *,
    tour="ATP",
    odd_p1=1.40,
    odd_p2=3.00,
    date="2026-06-11",
):
    return {
        "date": date,
        "time": "14:00",
        "player1": p1,
        "player2": p2,
        "odd_p1": odd_p1,
        "odd_p2": odd_p2,
        "true_odd_p1": odd_p1,
        "true_odd_p2": odd_p2,
        "tour": tour,
        "tournament": "Wimbledon",
        "tourney_winner_points": 500,
        "surface": "Grass",
        "feature_snapshot": {"capped_p1_prob": p1_prob},
    }


def test_passes_ev_band_inclusive():
    assert _passes_ev_band({"ev_fav_pct": 15.0}, ev_min_pct=15.0, ev_max_pct=100.0)
    assert _passes_ev_band({"ev_fav_pct": 100.0}, ev_min_pct=15.0, ev_max_pct=100.0)
    assert not _passes_ev_band({"ev_fav_pct": 14.9}, ev_min_pct=15.0, ev_max_pct=100.0)
    assert not _passes_ev_band({"ev_fav_pct": -3.9}, ev_min_pct=15.0, ev_max_pct=100.0)


def test_fallthrough_to_second_atp_when_rank1_ev_fails():
    """Medvedev-like rank=1 EV négatif → Fritz rank=2 devient candidat ATP."""
    medvedev = _match("Medvedev", "X", 0.89, odd_p1=1.08, odd_p2=8.0)
    fritz = _match("Fritz", "Landaluce", 0.75, odd_p1=1.63, odd_p2=2.40)
    wta_ok = _match("Swiatek", "Y", 0.82, tour="WTA", odd_p1=1.45, odd_p2=3.50)

    rows = collect_daily_top_proba_rows(
        [medvedev, fritz, wta_ok],
        calendar_date="2026-06-11",
        top_limit=15,
        today_only=True,
    )
    circuit = _first_ev_eligible_per_circuit(
        rows,
        ev_min_pct=15.0,
        ev_max_pct=100.0,
    )
    assert len(circuit) == 2
    atp = next(r for r in circuit if r["tour"] == "ATP")
    assert atp["fav_player"] == "Fritz"
    assert _passes_ev_band(atp, ev_min_pct=15.0, ev_max_pct=100.0)

    pick = _best_circuit_candidate_between_circuits(circuit)
    assert pick is not None
    assert pick["fav_player"] == "Swiatek"
    assert float(pick["p_model_fav"]) > float(atp["p_model_fav"])


def test_fritz_wins_when_highest_proba_among_eligible():
    """Si Fritz a la meilleure proba parmi les candidats EV, il est retenu."""
    medvedev = _match("Medvedev", "X", 0.89, odd_p1=1.08, odd_p2=8.0)
    fritz = _match("Fritz", "Landaluce", 0.91, odd_p1=1.30, odd_p2=3.80)
    wta_ok = _match("Swiatek", "Y", 0.82, tour="WTA", odd_p1=1.45, odd_p2=3.50)

    rows = collect_daily_top_proba_rows(
        [medvedev, fritz, wta_ok],
        calendar_date="2026-06-11",
        top_limit=15,
        today_only=True,
    )
    circuit = _first_ev_eligible_per_circuit(rows, ev_min_pct=15.0, ev_max_pct=100.0)
    pick = _best_circuit_candidate_between_circuits(circuit)
    assert pick is not None
    assert pick["fav_player"] == "Fritz"


def test_duplicate_snapshot_match_ranked_once():
    """Même match 2× dans le snapshot (IDs TE différents) → une seule ligne classée."""
    pridankina = _match(
        "Pridankina E.",
        "Avdeeva J.",
        0.75,
        tour="WTA",
        odd_p1=1.63,
        odd_p2=2.40,
    )
    pridankina["prematch_id"] = "te_618"
    dup = dict(pridankina)
    dup["prematch_id"] = "te_999"
    dup["odd_p1"] = 1.70
    dup["true_odd_p1"] = 1.70
    other = _match("Swiatek", "Y", 0.82, tour="WTA", odd_p1=1.45, odd_p2=3.50)

    rows = collect_daily_top_proba_rows(
        [pridankina, dup, other],
        calendar_date="2026-06-11",
        top_limit=15,
        today_only=True,
    )
    prid = [r for r in rows if "pridankina" in str(r.get("match_name", "")).lower()]
    assert len(prid) == 1
    wta = [r for r in rows if r["tour"] == "WTA"]
    assert len(wta) == 2
    assert wta[0]["fav_player"] == "Swiatek"
    assert wta[1]["fav_player"] == "Pridankina E."


def test_atp_tiebreak_when_equal_proba():
    atp = _match("A", "B", 0.80, odd_p1=1.50, odd_p2=2.80)
    wta = _match("C", "D", 0.80, tour="WTA", odd_p1=1.50, odd_p2=2.80)
    rows = collect_daily_top_proba_rows(
        [atp, wta],
        calendar_date="2026-06-11",
        top_limit=15,
        today_only=True,
    )
    circuit = _first_ev_eligible_per_circuit(rows, ev_min_pct=15.0, ev_max_pct=100.0)
    pick = _best_circuit_candidate_between_circuits(circuit)
    assert pick is not None
    assert pick["tour"] == "ATP"


def test_ev_plus_fallback_when_standard_pick_below_70_proba():
    """Standard pick WTA 54% EV 39% → repli EV+ : Bergs ATP 70% EV 4.5%."""
    from scripts.discord_1d1p_core import select_1d1p_pick

    rows = [
        {
            "tour": "ATP",
            "rank": 1,
            "fav_player": "Bergs Z.",
            "p_model_fav": 0.701,
            "ev_fav_pct": 4.5,
            "match_name": "Bergs Z. vs Samuel T.",
        },
        {
            "tour": "ATP",
            "rank": 2,
            "fav_player": "Borges N.",
            "p_model_fav": 0.649,
            "ev_fav_pct": 10.3,
            "match_name": "Borges N. vs Quinn E.",
        },
        {
            "tour": "WTA",
            "rank": 1,
            "fav_player": "Maria T.",
            "p_model_fav": 0.544,
            "ev_fav_pct": 39.2,
            "match_name": "Maria T. vs Ostapenko J.",
        },
    ]
    pick = select_1d1p_pick(rows, ev_min_pct=15.0, ev_max_pct=100.0)
    assert pick is not None
    assert pick["selection_mode"] == "ev_plus_fallback"
    assert pick["fav_player"] == "Bergs Z."
    assert float(pick["p_model_fav"]) >= 0.70
