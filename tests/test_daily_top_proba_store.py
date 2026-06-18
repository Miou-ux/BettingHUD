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


def test_collect_top5_excludes_challenger():
    from scripts.daily_top_proba_store import collect_top5_proba_picks

    major = _match("A", "B", 0.88, "WTA", date="2026-05-27")
    major["true_odd_p1"] = 1.20
    major["true_odd_p2"] = 5.00
    major["odd_p1"] = 1.25
    major["odd_p2"] = 4.50
    chal = dict(major)
    chal.update(
        {
            "player1": "C",
            "player2": "D",
            "tournament": "Foggia",
            "tourney_winner_points": 125,
            "feature_snapshot": {"capped_p1_prob": 0.95},
            "true_odd_p1": 1.15,
            "odd_p1": 1.18,
        }
    )
    picks = collect_top5_proba_picks(
        [chal, major],
        limit=5,
        ev_min_frac=0.0,
        ev_max_frac=2.0,
        today_only=True,
        calendar_date="2026-05-27",
    )
    assert len(picks) == 1
    assert picks[0]["fav_player"] == "A"
    assert "challenger" not in str(picks[0].get("tournament") or "").lower()


def test_collect_paris_du_jour_includes_challenger():
    from scripts.daily_top_proba_store import collect_paris_du_jour_picks

    major = _match("A", "B", 0.88, "WTA", date="2026-05-27")
    major["true_odd_p1"] = 1.20
    major["true_odd_p2"] = 5.00
    major["odd_p1"] = 1.25
    major["odd_p2"] = 4.50
    chal = dict(major)
    chal.update(
        {
            "player1": "C",
            "player2": "D",
            "tournament": "Foggia",
            "tourney_winner_points": 125,
            "feature_snapshot": {"capped_p1_prob": 0.95},
            "true_odd_p1": 1.15,
            "odd_p1": 1.18,
        }
    )
    picks = collect_paris_du_jour_picks(
        [chal, major],
        limit=None,
        ev_min_frac=0.0,
        ev_max_frac=2.0,
        today_only=True,
        calendar_date="2026-05-27",
    )
    assert len(picks) == 2
    assert picks[0]["fav_player"] == "C"
    assert picks[1]["fav_player"] == "A"


def test_filter_matches_for_daily_top_proba():
    from scripts.daily_top_proba_store import filter_matches_for_daily_top_proba

    ok = {
        "date": "2026-06-05",
        "odd_p1": 1.5,
        "odd_p2": 2.5,
        "p1_stats": {
            "stats_source": "matches_recent",
            "stats_reference_date": "2026-05-01",
        },
        "p2_stats": {
            "stats_source": "wta_matches",
            "stats_reference_date": "2026-05-01",
        },
    }
    bad_odds = {"odd_p1": 1.0, "odd_p2": 2.0, "p1_stats": ok["p1_stats"], "p2_stats": ok["p2_stats"]}
    bad_rank = {"odd_p1": 1.5, "odd_p2": 2.5, "p1_stats": {"stats_source": "tennisexplorer_estimate"}}
    stale = {
        "date": "2026-06-05",
        "odd_p1": 1.5,
        "odd_p2": 2.5,
        "p1_stats": {"stats_source": "matches_recent", "stats_reference_date": "2016-07-11"},
        "p2_stats": ok["p2_stats"],
    }
    out = filter_matches_for_daily_top_proba([ok, bad_odds, bad_rank, stale])
    assert len(out) == 1


if __name__ == "__main__":
    test_collect_top15_per_circuit()
    test_filter_matches_for_daily_top_proba()
    print("OK")
