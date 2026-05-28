"""Tests backfill daily top probas from algo_opportunities."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.daily_top_proba_store import _merge_algo_opportunity_match


def test_merge_algo_opportunity_match_favorite_from_side2():
    rows = [
        {
            "detected_date": "2026-05-18",
            "detected_ts": "2026-05-18T10:00:00+02:00",
            "match_date": "2026-05-18",
            "match_id": "te_1",
            "match_name": "A vs B",
            "player1": "A",
            "player2": "B (5)",
            "side": 2,
            "tour": "ATP",
            "surface": "Clay",
            "tournament": "RG",
            "odd_book": 1.67,
            "true_odd": 1.15,
            "p_model": 0.869,
            "confidence": 0.8,
            "segment_key": "ATP_Clay",
            "segment_brier": 0.18,
            "snapshot_tier": "full",
            "status": "Gagné",
        }
    ]
    merged = _merge_algo_opportunity_match(rows, calendar_date="2026-05-18")
    assert merged is not None
    assert merged["fav_player"] == "B (5)"
    assert round(merged["p_model_fav"], 3) == 0.869
    assert merged["capture_source"] == "backfill_algo_opportunities"


if __name__ == "__main__":
    test_merge_algo_opportunity_match_favorite_from_side2()
    print("OK")
