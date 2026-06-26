"""Tests QC live snapshot (surface + inactivité)."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.qc_live_snapshot import run_qc_live_snapshot

_TODAY = datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()


def _match(**kwargs):
    base = {
        "date": _TODAY,
        "time": "14:00",
        "tournament": "Bad Homburg WTA",
        "tournament_url": "/tournament/bad-homburg/",
        "surface": "Grass",
        "player1": "Muchova K.",
        "player2": "Ruse E.",
        "odd_p1": 1.47,
        "odd_p2": 2.8,
        "feature_snapshot": {
            "capped_p1_prob": 0.87,
            "p1_days_since_last_match": 3,
            "p2_days_since_last_match": 2,
        },
        "p1_match_quality": {"wins_last7d": 2},
        "p2_match_quality": {"wins_last7d": 3},
    }
    base.update(kwargs)
    return base


def test_surface_mismatch_blocking():
    m = _match(surface="Hard")
    rep = run_qc_live_snapshot([m])
    assert not rep.ok
    assert any(i.code == "surface_mismatch" for i in rep.blocking)


def test_inactivity_conflict_blocking():
    m = _match(
        feature_snapshot={
            "capped_p1_prob": 0.87,
            "p1_days_since_last_match": 32,
            "p2_days_since_last_match": 2,
        },
        p1_match_quality={"wins_last7d": 2},
    )
    rep = run_qc_live_snapshot([m])
    assert not rep.ok
    assert any(i.code == "inactivity_wins_conflict" for i in rep.blocking)


def test_clean_match_ok():
    rep = run_qc_live_snapshot([_match()])
    assert rep.ok
    assert not rep.blocking
