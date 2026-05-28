"""Tests priority_score composite (V47 segments)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.priority_scoring import (  # noqa: E402
    PREMIUM_SEGMENT_BRIER_MAX,
    is_premium_segment,
    priority_score_composite,
    sharpe_per_brier,
)
from scripts.value_detector import ValueDetector  # noqa: E402


class TestPriorityScoring(unittest.TestCase):
    def test_premium_threshold(self):
        self.assertTrue(is_premium_segment(0.17))
        self.assertFalse(is_premium_segment(0.18))
        self.assertFalse(is_premium_segment(0.22))

    def test_lower_brier_raises_priority(self):
        det = ValueDetector()
        res = det.detect_value(2.10, 1.80)
        sr = res["sharpe_ratio"]
        good = priority_score_composite(sr, 0.14)
        bad = priority_score_composite(sr, 0.22)
        self.assertGreater(good, bad)

    def test_formula_chain(self):
        sr = 0.15
        b = 0.20
        spb = sharpe_per_brier(sr, b)
        self.assertAlmostEqual(spb, sr / b, places=9)
        ps = priority_score_composite(sr, b)
        self.assertAlmostEqual(ps, spb * (1.0 - b / 0.25), places=9)

    def test_premium_constant(self):
        self.assertAlmostEqual(PREMIUM_SEGMENT_BRIER_MAX, 0.18)


if __name__ == "__main__":
    unittest.main()
