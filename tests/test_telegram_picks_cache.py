"""Picks Telegram precompute cache."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.pick_modes import Channel, PickMode, load_picks
from scripts.telegram_runtime_cache import (
    get_cached_pick_load_result,
    invalidate_snapshot_cache,
    store_cached_pick_load_result,
)


def test_pick_cache_roundtrip():
    invalidate_snapshot_cache()
    sample = load_picks.__wrapped__ if hasattr(load_picks, "__wrapped__") else None
    from scripts.pick_modes import PickLoadResult

    result = PickLoadResult(
        picks=[{"rank": 1, "match_name": "A vs B"}],
        meta={"built_at": 1.0},
        calendar_date="2026-06-16",
        pool_n=10,
        snapshot_age_min=0.0,
        mode=PickMode.TOP5,
    )
    store_cached_pick_load_result(
        result,
        PickMode.TOP5,
        Channel.TELEGRAM,
        limit=None,
        ev_min_pct=15.0,
        ev_max_pct=100.0,
    )
    hit = get_cached_pick_load_result(
        PickMode.TOP5,
        Channel.TELEGRAM,
        limit=None,
        ev_min_pct=15.0,
        ev_max_pct=100.0,
    )
    assert hit is not None
    assert hit.picks[0]["match_name"] == "A vs B"
    hit.picks[0]["match_name"] = "mutated"
    hit2 = get_cached_pick_load_result(
        PickMode.TOP5,
        Channel.TELEGRAM,
        limit=None,
        ev_min_pct=15.0,
        ev_max_pct=100.0,
    )
    assert hit2 is not None
    assert hit2.picks[0]["match_name"] == "A vs B"


if __name__ == "__main__":
    os.environ.setdefault("TELEGRAM_PICKS_CACHE", "1")
    test_pick_cache_roundtrip()
    print("ok")
