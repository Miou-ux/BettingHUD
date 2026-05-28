"""Tests collect_daily_top_proba_rows."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.daily_top_proba_store import collect_daily_top_proba_rows


def _match(p1, p2, p1_prob, tour="WTA", date="2026-05-27"):
    return {
        "date": date,
        "time": "14:00",
        "player1": p1,
        "player2": p2,
        "odd_p1": 1.40,
        "odd_p2": 3.00,
        "true_odd_p1": 1.35,
        "true_odd_p2": 3.20,
        "tour": tour,
        "tournament": "Roland Garros",
        "surface": "Clay",
        "feature_snapshot": {"capped_p1_prob": p1_prob},
    }


def test_collect_top15_per_circuit():
    matches = [
        _match("A", "B", 0.90, "WTA"),
        _match("C", "D", 0.80, "WTA"),
        _match("E", "F", 0.70, "ATP"),
        _match("G", "H", 0.60, "ATP"),
    ]
    rows = collect_daily_top_proba_rows(
        matches, calendar_date="2026-05-27", top_limit=15, today_only=True
    )
    wta = [r for r in rows if r["tour"] == "WTA"]
    atp = [r for r in rows if r["tour"] == "ATP"]
    assert len(wta) == 2
    assert len(atp) == 2
    assert wta[0]["rank"] == 1 and wta[0]["p_model_fav"] == 0.90
    assert wta[1]["rank"] == 2
    assert atp[0]["fav_player"] == "E"


def test_filter_matches_for_daily_top_proba():
    from scripts.daily_top_proba_store import filter_matches_for_daily_top_proba

    ok = {
        "odd_p1": 1.5,
        "odd_p2": 2.5,
        "p1_stats": {"stats_source": "matches_recent"},
        "p2_stats": {"stats_source": "wta_matches"},
    }
    bad_odds = {"odd_p1": 1.0, "odd_p2": 2.0, "p1_stats": ok["p1_stats"], "p2_stats": ok["p2_stats"]}
    bad_rank = {"odd_p1": 1.5, "odd_p2": 2.5, "p1_stats": {"stats_source": "tennisexplorer_estimate"}}
    out = filter_matches_for_daily_top_proba([ok, bad_odds, bad_rank])
    assert len(out) == 1


if __name__ == "__main__":
    test_collect_top15_per_circuit()
    test_filter_matches_for_daily_top_proba()
    print("OK")
