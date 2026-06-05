"""Tests garde-fous rang/points (fraîcheur TML/WTA)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.match_rank_quality import (
    match_has_rank_points_source,
    match_rank_exclude_reason,
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


if __name__ == "__main__":
    test_fresh_match_eligible()
    test_stale_tml_excluded()
    test_missing_ref_date_excluded()
    test_tennisexplorer_estimate_excluded()
    print("OK")
