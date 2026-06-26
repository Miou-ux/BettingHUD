"""Inactivité live : jours depuis DB, pas feature store."""
from __future__ import annotations

import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.stats_engine import get_days_since_last_match


def test_days_from_db_not_stale_store():
    ref = pd.Timestamp("2026-06-26")
    rows = pd.DataFrame(
        {
            "tourney_date": [pd.Timestamp("2026-06-24"), pd.Timestamp("2026-06-20")],
        }
    )
    assert get_days_since_last_match(rows, ref) == 2
    stale_store_days = 32
    assert get_days_since_last_match(rows, ref) != stale_store_days
