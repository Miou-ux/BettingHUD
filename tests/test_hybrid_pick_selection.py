"""Tests HYB P75+P80-all prod selection."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.hyb_p75_p80_selection import (
    best_1d1p_pick_from_hyb,
    select_hyb_p75_p80_all,
)
from scripts.hybrid_pick_selection import select_hybrid_picks, select_hybrid_picks_legacy


def _row(
    p1: str,
    p2: str,
    p: float,
    ev: float,
    *,
    rel: int = 90,
    tour: str = "ATP",
    gap: float | None = 10.0,
) -> dict:
    return {
        "match_name": f"{p1} vs {p2}",
        "player1": p1,
        "player2": p2,
        "calendar_date": "2026-07-03",
        "tour": tour,
        "tournament": "Wimbledon",
        "p_model_fav": p,
        "ev_fav_pct": ev,
        "data_reliability_score": rel,
        "book_gap_pp": gap,
        "theoretical_stake_frac": 0.03,
    }


def test_p80_addon_included_without_ev_filter():
    rows = [
        _row("Star", "A", 0.82, 20.0),  # P75 + P80
        _row("Fav", "B", 0.81, -5.0, rel=85),  # P80 only (EV neg), not P75 (ev band)
    ]
    picks = select_hybrid_picks(rows, apply_telegram_proba_filter=False)
    names = {p["player1"] for p in picks}
    assert "Star" in names
    assert "Fav" in names


def test_1d1p_best_proba_not_first_rank():
    rows = [
        _row("Lower", "A", 0.88, 20.0),
        _row("Higher", "B", 0.92, -2.0),
    ]
    picks = select_hyb_p75_p80_all(rows)
    best = best_1d1p_pick_from_hyb(picks)
    assert best is not None
    assert best["player1"] == "Higher"
    assert best["selection_mode"] == "hyb_p75_p80_best_proba"


def test_dedupe_match_between_p75_and_p80():
    rows = [_row("Same", "X", 0.85, 22.0)]
    picks = select_hyb_p75_p80_all(rows)
    assert len(picks) == 1


def test_legacy_hybrid_still_available():
    rows = [
        _row("Ok", "B", 0.77, 22.0),
        _row("Weak", "A", 0.76, 20.0),
    ]
    picks = select_hybrid_picks_legacy(rows, limit=5, apply_telegram_proba_filter=False)
    assert len(picks) == 1
    assert picks[0]["player1"] == "Ok"


def test_hybrid_criteria_plain_mentions_p75_p80():
    from scripts.hybrid_pick_selection import hybrid_criteria_plain

    fr = hybrid_criteria_plain(english=False)
    assert "P75-TIER" in fr or "P75" in fr
    assert "80" in fr
