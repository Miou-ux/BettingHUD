"""Résolution clé Brier segment live (WTA terre, etc.)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ml_model import resolve_match_brier_segment_key, resolve_segment_brier_score


class _FakeMl:
    segment_brier_scores = {
        "WTA_Clay": 0.138,
        "WTA_Clay_G": 0.135,
        "tour_WTA": 0.161,
    }
    global_test_brier = 0.174


def test_wta_french_open_resolves_wta_clay_g():
    ml = _FakeMl()
    key = resolve_match_brier_segment_key(
        ml,
        tour="WTA",
        surface="Clay",
        tournament="French Open (WTA)",
        tourney_level="G",
    )
    assert key == "WTA_Clay_G"


def test_dual_bo3_key_falls_back_to_global():
    ml = _FakeMl()
    assert resolve_segment_brier_score(ml, "dual_bo3") == 0.174


def test_wta_clay_brier_not_global():
    ml = _FakeMl()
    key = resolve_match_brier_segment_key(ml, tour="WTA", surface="Clay", tournament="Rome")
    assert key == "WTA_Clay"
    assert resolve_segment_brier_score(ml, key) == 0.138
