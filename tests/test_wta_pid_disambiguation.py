"""WTA homonym disambiguation (e.g. Leylah vs Lya Fernandez)."""
from __future__ import annotations

import pandas as pd
import pytest

from scripts.stats_engine import TennisStatsEngine


class _StubStatsEngine(TennisStatsEngine):
    """Minimal stub to test disambiguation keys without DB."""

    def __init__(self):
        pass

    def _last_wta_match(self, pid_int: int):
        return self._stub_matches.get(pid_int, (None, None))

    def _wta_rankings_current_meta(self, pid_int: int):
        return self._stub_rankings.get(pid_int)


def test_nan_match_rank_falls_back_to_current_rankings():
    se = _StubStatsEngine()
    se._stub_matches = {
        220367: (
            pd.Series(
                {
                    "winner_name": "Leylah Fernandez",
                    "winner_rank": float("nan"),
                    "tourney_date": "20260621",
                    "tourney_level": "G",
                    "tourney_name": "Bad Homburg",
                }
            ),
            True,
        ),
        248660: (
            pd.Series(
                {
                    "loser_name": "Lya Fernandez",
                    "loser_rank": 964,
                    "tourney_date": "20230918",
                    "tourney_level": "I",
                    "tourney_name": "Guadalajara ITF",
                }
            ),
            False,
        ),
    }
    se._stub_rankings = {
        220367: (23, 2261.0, "20260624"),
        248660: (933, 26.0, "20250624"),
    }
    te = "https://www.tennisexplorer.com/player/fernandez-8411c/"
    key_leylah = se._wta_pid_disambiguation_key(220367, se._te_player_slug_from_url(te))
    key_lya = se._wta_pid_disambiguation_key(248660, se._te_player_slug_from_url(te))
    assert key_leylah > key_lya


def test_pick_wta_pid_prefers_leylah_over_lya():
    se = _StubStatsEngine()
    se._stub_matches = {
        220367: (
            pd.Series(
                {
                    "winner_name": "Leylah Fernandez",
                    "winner_rank": float("nan"),
                    "tourney_date": "20260621",
                    "tourney_level": "G",
                    "tourney_name": "Bad Homburg",
                }
            ),
            True,
        ),
        248660: (
            pd.Series(
                {
                    "loser_name": "Lya Fernandez",
                    "loser_rank": 964,
                    "tourney_date": "20230918",
                    "tourney_level": "I",
                    "tourney_name": "Guadalajara ITF",
                }
            ),
            False,
        ),
    }
    se._stub_rankings = {
        220367: (23, 2261.0, "20260624"),
        248660: (933, 26.0, "20250624"),
    }
    te = "https://www.tennisexplorer.com/player/fernandez-8411c/"
    picked = se._pick_wta_pid_candidate([248660, 220367], te)
    assert picked == 220367
