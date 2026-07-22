"""Tests sélection hybride prod."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.hybrid_pick_selection import select_hybrid_picks


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
    }


def test_hybrid_tier1_sorted_by_proba():
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
        _row("TwoA", "X", 0.90, 35.0),  # tier1 (15–35)
        _row("TwoB", "Y", 0.81, 45.0),  # tier2
    ]
    picks = select_hybrid_picks(rows, limit=3, apply_telegram_proba_filter=False)
    assert len(picks) == 3
    assert picks[0]["player1"] == "TwoA"
    assert picks[0]["hybrid_tier"] == "tier1"
    assert picks[1]["player1"] == "Only"
    assert picks[2]["player1"] == "TwoB"
    assert picks[2]["hybrid_tier"] == "tier2"


def test_hybrid_accepts_combo_volume_ev_bands():
    """COMBO_VOLUME prod : tier1 jusqu'à 35 %, tier2 jusqu'à 55 %."""
    rows = [
        _row("T1Edge", "A", 0.80, 32.0),
        _row("T2Edge", "B", 0.81, 52.0),
        _row("T2Mid", "C", 0.82, 36.0),  # tier2 only (>35)
        _row("OverPool", "D", 0.83, 56.0),  # hors pool (>55)
    ]
    picks = select_hybrid_picks(rows, limit=5, apply_telegram_proba_filter=False)
    names = {p["player1"] for p in picks}
    assert "T1Edge" in names
    assert "T2Edge" in names
    assert "T2Mid" in names
    assert picks[[p["player1"] for p in picks].index("T2Mid")]["hybrid_tier"] == "tier2"
    assert "OverPool" not in names


def test_hybrid_rejects_below_77_proba():
    rows = [_row("Weak", "A", 0.76, 20.0), _row("Ok", "B", 0.77, 22.0)]
    picks = select_hybrid_picks(rows, limit=5, apply_telegram_proba_filter=False)
    assert len(picks) == 1
    assert picks[0]["player1"] == "Ok"


def test_hybrid_rel_fallback_when_primary_empty():
    """Si rel≥85 ne retourne rien, repli rel≥80."""
    rows = [
        _row("A", "X", 0.85, 20.0, rel=80),
        _row("B", "Y", 0.78, 22.0, rel=70),
    ]
    picks = select_hybrid_picks(rows, limit=6, apply_telegram_proba_filter=False)
    assert len(picks) == 1
    assert picks[0]["player1"] == "A"
    assert picks[0].get("hybrid_rel_fallback") is True
    assert picks[0].get("hybrid_rel_min") == 80


def test_hybrid_no_fallback_when_primary_has_picks():
    rows = [
        _row("Strong", "X", 0.88, 18.0, rel=90),
        _row("Weak", "Y", 0.85, 20.0, rel=80),
    ]
    picks = select_hybrid_picks(rows, limit=6, apply_telegram_proba_filter=False)
    assert len(picks) == 1
    assert picks[0]["player1"] == "Strong"
    assert "hybrid_rel_fallback" not in picks[0]


def test_hybrid_rejects_high_book_gap():
    rows = [
        _row("GapOk", "A", 0.80, 20.0, gap=25.0),
        _row("GapBad", "B", 0.82, 24.0, gap=35.0),
    ]
    picks = select_hybrid_picks(rows, limit=5, apply_telegram_proba_filter=False)
    assert len(picks) == 1
    assert picks[0]["player1"] == "GapOk"


def test_hybrid_criteria_plain_mentions_fallback():
    from scripts.hybrid_pick_selection import hybrid_criteria_plain

    fr = hybrid_criteria_plain(english=False)
    en = hybrid_criteria_plain(english=True)
    assert "repli ≥ 80" in fr
    assert "fallback >= 80" in en
