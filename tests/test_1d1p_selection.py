"""Tests sélection 1 Day 1 Pick — logique hybride."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.discord_1d1p_core import select_1d1p_pick


def _row(p1: str, p2: str, p: float, ev: float, *, tour: str = "ATP") -> dict:
    return {
        "match_name": f"{p1} vs {p2}",
        "player1": p1,
        "player2": p2,
        "calendar_date": "2026-07-03",
        "tour": tour,
        "tournament": "Wimbledon",
        "fav_player": p1,
        "p_model_fav": p,
        "ev_fav_pct": ev,
        "data_reliability_score": 90,
        "book_gap_pp": 10.0,
    }


def test_1d1p_is_best_hybrid_pick():
    rows = [
        _row("Tier2High", "X", 0.92, 35.0),
        _row("Tier1Best", "Y", 0.88, 22.0, tour="WTA"),
        _row("Tier1Low", "Z", 0.81, 18.0),
    ]
    pick = select_1d1p_pick(rows)
    assert pick is not None
    assert pick["selection_mode"] == "hybrid_best"
    assert pick["fav_player"] == "Tier1Best"


def test_1d1p_none_when_no_hybrid_candidate():
    rows = [_row("Low", "A", 0.76, 20.0)]
    assert select_1d1p_pick(rows) is None
