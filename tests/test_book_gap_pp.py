"""Définition canonique book_gap_pp (modèle vs book, favori modèle)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.match_rank_quality import (
    BOOK_GAP_HIGH_PP,
    book_gap_pp_from_favorite,
    book_gap_pp_from_match,
    match_data_reliability_score,
)


def test_book_gap_muchova_like():
    # ~93.3 % modèle, cote fav 1.47 → book ~68.0 %
    gap = book_gap_pp_from_favorite(0.933, 1.47)
    assert gap is not None
    assert 24.0 < gap < 27.0


def test_book_gap_from_match_snapshot():
    m = {
        "player1": "Muchova K.",
        "player2": "Ruse E.",
        "odd_p1": 1.47,
        "odd_p2": 2.80,
        "feature_snapshot": {"capped_p1_prob": 0.933},
    }
    gap = book_gap_pp_from_match(m)
    assert gap is not None
    assert gap == book_gap_pp_from_favorite(0.933, 1.47)


def test_book_gap_not_vig_margin():
    """Ancienne formule (cote publique vs true_odd) — ne doit plus être book_gap_pp."""
    m = {
        "odd_p1": 1.47,
        "odd_p2": 2.80,
        "true_odd_p1": 1.52,
        "true_odd_p2": 2.90,
        "feature_snapshot": {"capped_p1_prob": 0.55},
    }
    gap = book_gap_pp_from_match(m)
    vig_only = abs(1.0 / 1.47 - 1.0 / 1.52) * 100.0
    assert gap is not None
    assert abs(gap - vig_only) > 1.0


def test_book_gap_high_flag_uses_canonical_gap():
    m = {
        "date": "2026-06-26",
        "player1": "A",
        "player2": "B",
        "odd_p1": 1.47,
        "odd_p2": 3.0,
        "p1_player_id": "WTA::1",
        "p2_player_id": "WTA::2",
        "p1_stats": {"rank": 10, "pts": 4000, "stats_source": "wta_matches", "stats_reference_date": "2026-06-01"},
        "p2_stats": {"rank": 50, "pts": 1000, "stats_source": "wta_matches", "stats_reference_date": "2026-06-01"},
        "feature_snapshot": {"capped_p1_prob": 0.933},
        "book_gap_pp": book_gap_pp_from_match(
            {
                "odd_p1": 1.47,
                "odd_p2": 3.0,
                "feature_snapshot": {"capped_p1_prob": 0.933},
            }
        ),
    }
    score, flags = match_data_reliability_score(m)
    if m["book_gap_pp"] and m["book_gap_pp"] > BOOK_GAP_HIGH_PP:
        assert "book_gap_high" in flags
    else:
        assert "book_gap_high" not in flags
