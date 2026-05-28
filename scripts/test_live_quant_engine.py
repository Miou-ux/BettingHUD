"""Tests légers pour le moteur In-Play."""
from __future__ import annotations

import unittest

from scripts.live_quant_engine import (
    build_manual_score_state,
    calculate_live_probability,
    is_end_of_set,
    is_first_set_in_progress,
    parse_live_score_state,
)


class LiveQuantEngineTests(unittest.TestCase):
    def test_prior_drops_after_losing_first_set_bo3(self):
        prior = 0.60
        live = calculate_live_probability(prior, (0, 1), best_of=3)
        self.assertLess(live.live_proba, prior)

    def test_is_end_of_set_set_break_flag(self):
        state = build_manual_score_state(sets_p1=1, sets_p2=0, status_set_break=True)
        self.assertTrue(is_end_of_set(state))

    def test_is_end_of_set_game_zero_after_set(self):
        state = build_manual_score_state(sets_p1=1, sets_p2=0, game_p1=0, game_p2=0)
        self.assertTrue(is_end_of_set(state))

    def test_not_end_of_set_match_start(self):
        state = build_manual_score_state(sets_p1=0, sets_p2=0, game_p1=0, game_p2=0)
        self.assertFalse(is_end_of_set(state))

    def test_not_end_of_set_mid_set(self):
        state = build_manual_score_state(sets_p1=1, sets_p2=0, game_p1=2, game_p2=1)
        self.assertFalse(is_end_of_set(state))

    def test_first_set_in_progress_with_games(self):
        state = build_manual_score_state(sets_p1=0, sets_p2=0, game_p1=2, game_p2=1)
        self.assertTrue(is_first_set_in_progress(state))

    def test_first_set_in_progress_at_zero_requires_flag(self):
        state = build_manual_score_state(sets_p1=0, sets_p2=0, game_p1=0, game_p2=0)
        self.assertFalse(is_first_set_in_progress(state))
        started = build_manual_score_state(
            sets_p1=0,
            sets_p2=0,
            game_p1=0,
            game_p2=0,
            status_first_set_in_progress=True,
        )
        self.assertTrue(is_first_set_in_progress(started))

    def test_not_first_set_after_completed_set(self):
        state = build_manual_score_state(sets_p1=1, sets_p2=0, game_p1=0, game_p2=0)
        self.assertFalse(is_first_set_in_progress(state))

    def test_parse_string_score(self):
        state = parse_live_score_state("sets 1-0, game 0-0, Set Break")
        self.assertEqual(state["sets_p1"], 1)
        self.assertEqual(state["sets_p2"], 0)
        self.assertTrue(state["status_set_break"])

    def test_ev_live_positive_with_edge(self):
        live = calculate_live_probability(0.55, (1, 0), best_of=3, live_odd=2.20)
        self.assertIsNotNone(live.ev_live)
        assert live.ev_live is not None
        self.assertGreater(live.ev_live, 0.0)


if __name__ == "__main__":
    unittest.main()
