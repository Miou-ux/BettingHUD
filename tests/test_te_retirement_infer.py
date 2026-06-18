"""TE retirement inference when score cells omit 'ret.'."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.scraper_results import (
    _infer_te_retirement,
    _winner_from_incomplete_last_set,
    score_suggests_retirement,
)


def test_mboko_style_tied_sets_partial_last_set():
    full = "6 3 | 2 4"
    assert score_suggests_retirement(full)
    assert _infer_te_retirement(
        p1_res="1",
        p2_res="1",
        full_score=full,
        sets_p1=1,
        sets_p2=1,
    )
    assert _winner_from_incomplete_last_set(full, "Mboko", "Pliskova") == "Pliskova"


def test_complete_match_not_retired():
    assert not _infer_te_retirement(
        p1_res="2",
        p2_res="0",
        full_score="6 4 | 6 2",
        sets_p1=2,
        sets_p2=0,
    )
    assert not score_suggests_retirement("6 6 | 4 4")
    assert not _infer_te_retirement(
        p1_res="2",
        p2_res="0",
        full_score="6 6 | 4 4",
        sets_p1=2,
        sets_p2=0,
    )
def test_ret_marker_in_result_column():
    assert _infer_te_retirement(
        p1_res="1 ret.",
        p2_res="2",
        full_score="6 3 | 6 4",
        sets_p1=1,
        sets_p2=2,
    )
