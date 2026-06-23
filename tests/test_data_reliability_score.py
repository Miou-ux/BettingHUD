"""Tests score de fiabilité données (match_rank_quality)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.match_rank_quality import match_data_reliability_score


def _stats(source: str, ref: str, rank: int = 50, pts: float = 500.0) -> dict:
    return {
        "stats_source": source,
        "stats_reference_date": ref,
        "rank": rank,
        "pts": pts,
    }


def test_clean_match_high_score():
    m = {
        "date": "2026-06-23",
        "p1_player_id": "WTA::1",
        "p2_player_id": "WTA::2",
        "p1_stats": _stats("wta_matches", "2026-06-01"),
        "p2_stats": _stats("wta_matches", "2026-06-01"),
        "snapshot_tier": "full",
        "book_gap_pp": 8.0,
    }
    score, flags = match_data_reliability_score(m)
    assert score >= 90
    assert not flags


def test_rank_vs_proba_heavy_penalty():
    m = {
        "date": "2026-06-23",
        "unreliable": True,
        "p1_player_id": "ATP::1",
        "p2_player_id": "ATP::2",
        "p1_stats": _stats("matches_recent", "2026-06-01", rank=66),
        "p2_stats": _stats("matches_recent", "2026-06-01", rank=28),
        "snapshot_tier": "full",
    }
    score, flags = match_data_reliability_score(m)
    assert score <= 65
    assert "rang_vs_proba" in flags


def test_rank_placeholder_penalty():
    m = {
        "date": "2026-06-23",
        "p1_player_id": "WTA::1",
        "p2_player_id": "WTA::2",
        "p1_stats": _stats("wta_matches", "2026-06-01", rank=192),
        "p2_stats": _stats("rankings_wta_current", "2026-06-01", rank=1500, pts=3.0),
        "snapshot_tier": "full",
    }
    score, flags = match_data_reliability_score(m)
    assert "p2_rank_placeholder" in flags
    assert score < 90


def test_passes_data_reliability_filter():
    from scripts.match_rank_quality import (
        MIN_DATA_RELIABILITY_SCORE,
        passes_data_reliability_filter,
    )

    assert passes_data_reliability_filter({"data_reliability_score": 85})
    assert not passes_data_reliability_filter({"data_reliability_score": 79})
    assert not passes_data_reliability_filter(
        {"data_reliability_score": 90, "unreliable": True}
    )
    assert not passes_data_reliability_filter({})
    assert MIN_DATA_RELIABILITY_SCORE == 80


if __name__ == "__main__":
    test_clean_match_high_score()
    test_rank_vs_proba_heavy_penalty()
    test_rank_placeholder_penalty()
    test_passes_data_reliability_filter()
    print("OK")
