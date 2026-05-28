"""Tests du rapport de Sharpe unitaire (ValueDetector)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.value_detector import ValueDetector  # noqa: E402


class TestBetSharpeRatio(unittest.TestCase):
    def test_positive_value_has_positive_sharpe(self):
        det = ValueDetector(min_value_threshold=0.03)
        res = det.detect_value(bookmaker_odd=2.10, true_odd=1.80)
        self.assertTrue(res["is_value"])
        self.assertGreater(res["sharpe_ratio"], 0.0)

    def test_negative_ev_has_non_positive_sharpe(self):
        res = ValueDetector().detect_value(bookmaker_odd=1.70, true_odd=1.80)
        self.assertFalse(res["is_value"])
        self.assertLessEqual(res["sharpe_ratio"], 0.0)

    def test_invalid_odds_return_zero_sharpe(self):
        res = ValueDetector().detect_value(bookmaker_odd=1.0, true_odd=1.80)
        self.assertEqual(res["sharpe_ratio"], 0.0)

    def test_sharpe_can_reorder_vs_ev_pct(self):
        """Le Sharpe peut classer au-dessus un pari à EV % plus faible mais moins volatile."""
        det = ValueDetector()
        moderate = det.detect_value(bookmaker_odd=2.50, true_odd=2.10)
        aggressive = det.detect_value(bookmaker_odd=3.00, true_odd=2.50)
        self.assertGreater(aggressive["value_pct"], moderate["value_pct"])
        self.assertGreater(moderate["sharpe_ratio"], aggressive["sharpe_ratio"])

    def test_static_matches_detect_value(self):
        p = 1.0 / 1.80
        static = ValueDetector.bet_sharpe_ratio(p, 2.10)
        dynamic = ValueDetector().detect_value(2.10, 1.80)["sharpe_ratio"]
        self.assertAlmostEqual(static, dynamic, places=9)


if __name__ == "__main__":
    unittest.main()
