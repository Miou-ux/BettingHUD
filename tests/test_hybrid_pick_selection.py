"""Tests sélection hybride prod."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.hybrid_pick_selection import select_hybrid_picks


def _row(p1: str, p2: str, p: float, ev: float, *, rel: int = 90, tour: str = "ATP") -> dict:
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
    }


def test_hybrid_tier1_before_tier2_and_cap():
    rows = [
        _row("Low", "T2", 0.95, 40.0),
        _row("High", "T1", 0.88, 20.0),
        _row("Mid", "T1", 0.85, 25.0),
    ]
    picks = select_hybrid_picks(rows, limit=2, apply_telegram_proba_filter=False)
    assert len(picks) == 2
    assert picks[0]["player1"] == "High"
    assert picks[0]["hybrid_tier"] == "tier1"
    assert picks[1]["player1"] == "Mid"


def test_hybrid_tier2_fills_when_tier1_sparse():
    rows = [
        _row("Only", "T1", 0.82, 18.0),
        _row("TwoA", "X", 0.90, 35.0),
        _row("TwoB", "Y", 0.81, 45.0),
    ]
    picks = select_hybrid_picks(rows, limit=3, apply_telegram_proba_filter=False)
    assert len(picks) == 3
    assert picks[0]["player1"] == "Only"
    assert picks[1]["player1"] == "TwoA"
    assert picks[2]["hybrid_tier"] == "tier2"


def test_hybrid_rejects_below_80_proba():
    rows = [_row("Weak", "A", 0.79, 20.0), _row("Ok", "B", 0.81, 22.0)]
    picks = select_hybrid_picks(rows, limit=5, apply_telegram_proba_filter=False)
    assert len(picks) == 1
    assert picks[0]["player1"] == "Ok"
