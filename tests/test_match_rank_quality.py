"""Tests garde-fous rang/points (fraîcheur TML/WTA)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.match_rank_quality import (
    match_has_rank_points_source,
    match_rank_exclude_reason,
    passes_public_pick_gates,
    player_rank_stats_fresh,
)


def _stats(source: str, ref: str) -> dict:
    return {"stats_source": source, "stats_reference_date": ref}


def test_fresh_match_eligible():
    m = {
        "date": "2026-06-05",
        "p1_stats": _stats("matches_recent", "2026-05-01"),
        "p2_stats": _stats("matches_recent", "2026-04-15"),
    }
    assert match_has_rank_points_source(m)
    assert match_rank_exclude_reason(m) is None


def test_stale_tml_excluded():
    m = {
        "date": "2026-06-05",
        "p1_stats": _stats("matches_recent", "2016-07-11"),
        "p2_stats": _stats("matches_recent", "2026-05-01"),
    }
    assert not match_has_rank_points_source(m)
    assert match_rank_exclude_reason(m) == "stale_rank_stats"


def test_missing_ref_date_excluded():
    m = {
        "date": "2026-06-05",
        "p1_stats": {"stats_source": "matches_recent"},
        "p2_stats": _stats("wta_matches", "2026-05-01"),
    }
    assert not player_rank_stats_fresh(m["p1_stats"], anchor_date="2026-06-05")
    assert match_rank_exclude_reason(m) == "stale_rank_stats"


def test_tennisexplorer_estimate_excluded():
    m = {
        "date": "2026-06-05",
        "p1_stats": _stats("tennisexplorer_estimate", "2026-06-01"),
        "p2_stats": _stats("matches_recent", "2026-05-01"),
    }
    assert match_rank_exclude_reason(m) == "tennisexplorer_estimate"


def test_default_stats_placeholder_excluded():
    from scripts.match_rank_quality import is_default_player_stats, match_data_reliability_score

    default = {
        "rank": 100,
        "pts": 1000,
        "stats_source": "no_ranking_source",
    }
    assert is_default_player_stats(default)
    m = {
        "date": "2026-06-29",
        "p1_stats": dict(default),
        "p2_stats": dict(default),
    }
    assert match_rank_exclude_reason(m) == "default_stats_placeholder"
    score, flags = match_data_reliability_score(m)
    assert score < 80
    assert "p1_default_model_stats" in flags
    assert "p2_default_model_stats" in flags


def test_single_side_default_stats_excluded():
    real = {
        "rank": 86,
        "pts": 737,
        "stats_source": "matches_recent",
        "stats_reference_date": "2026-07-20",
    }
    default = {
        "rank": 100,
        "pts": 1000,
        "stats_source": "matches_recent",
        "stats_reference_date": "2026-07-20",
    }
    m = {
        "date": "2026-07-23",
        "player1": "Van Assche L.",
        "player2": "Carreno-Busta P.",
        "p1_stats": real,
        "p2_stats": default,
        "feature_snapshot": {"capped_p1_prob": 0.35},
        "odd_p1": 2.5,
        "odd_p2": 1.55,
        "data_reliability_score": 82,
    }
    assert match_rank_exclude_reason(m) == "default_stats_placeholder"
    assert not match_has_rank_points_source(m)
    assert not passes_public_pick_gates(m, min_score=80)


def test_rank_points_default_source_detected():
    from scripts.match_rank_quality import is_default_player_stats

    imputed = {
        "rank": 100,
        "pts": 1000,
        "stats_source": "rank_points_default",
        "stats_reference_date": "2026-07-20",
    }
    assert is_default_player_stats(imputed)
    m = {
        "date": "2026-07-23",
        "p1_stats": {
            "rank": 10,
            "pts": 3000,
            "stats_source": "matches_recent",
            "stats_reference_date": "2026-07-20",
        },
        "p2_stats": imputed,
    }
    assert match_rank_exclude_reason(m) == "default_stats_placeholder"


def test_duplicate_model_prob_penalizes_score():
    from scripts.match_rank_quality import (
        duplicate_model_prob_keys,
        match_data_reliability_score,
        match_in_duplicate_model_prob_cluster,
    )

    m1 = {
        "date": "2026-06-29",
        "player1": "A",
        "player2": "B",
        "p1_player_id": "1",
        "p2_player_id": "2",
        "feature_snapshot": {"capped_p1_prob": 0.62},
        "p1_stats": {
            "rank": 10,
            "pts": 3000,
            "stats_source": "matches_recent",
            "stats_reference_date": "2026-06-28",
        },
        "p2_stats": {
            "rank": 20,
            "pts": 2000,
            "stats_source": "matches_recent",
            "stats_reference_date": "2026-06-28",
        },
    }
    m2 = {
        **m1,
        "player1": "C",
        "player2": "D",
        "p1_player_id": "3",
        "p2_player_id": "4",
    }
    dup_keys = duplicate_model_prob_keys([m1, m2])
    assert len(dup_keys) == 2
    s_ok, flags_ok = match_data_reliability_score(m1, duplicate_model_prob=False)
    s_dup, flags_dup = match_data_reliability_score(
        m1, duplicate_model_prob=match_in_duplicate_model_prob_cluster(m1, dup_keys)
    )
    assert s_dup == s_ok - 20
    assert s_dup == 80
    assert "duplicate_model_prob" in flags_dup


if __name__ == "__main__":
    test_fresh_match_eligible()
    test_stale_tml_excluded()
    test_missing_ref_date_excluded()
    test_tennisexplorer_estimate_excluded()
    test_default_stats_placeholder_excluded()
    test_single_side_default_stats_excluded()
    test_rank_points_default_source_detected()
    test_duplicate_model_prob_penalizes_score()
    print("OK")
