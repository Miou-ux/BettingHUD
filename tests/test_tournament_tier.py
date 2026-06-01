"""Tests classification tournoi main draw vs 125 / Challenger."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.tournament_tier import (
    is_challenger_tier_match,
    is_main_draw_tournament_match,
)


def test_foggia_wta_125_excluded_from_main_draw():
    m = {
        "category": "WTA",
        "tournament": "Foggia",
        "tournament_url": "/foggia/2026/wta-women/",
        "tourney_winner_points": 125,
    }
    assert not is_main_draw_tournament_match(m)
    assert is_challenger_tier_match(m)


def test_challenger_category_from_scraper():
    m = {
        "category": "Challenger",
        "tournament": "Perugia challenger",
        "tournament_url": "/perugia/2026/atp-men/",
        "tourney_winner_points": 125,
    }
    assert is_challenger_tier_match(m)
    assert not is_main_draw_tournament_match(m)


def test_roland_garros_main_draw():
    m = {
        "category": "WTA",
        "tournament": "Roland Garros",
        "tourney_winner_points": 2000,
    }
    assert is_main_draw_tournament_match(m)
    assert not is_challenger_tier_match(m)


if __name__ == "__main__":
    test_foggia_wta_125_excluded_from_main_draw()
    test_roland_garros_main_draw()
    print("OK")
