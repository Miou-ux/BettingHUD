#!/usr/bin/env python3
"""Smoke prod HYB P75+P80-all + 1D1P best proba."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.discord_1d1p_core import load_1d1p_today_pick, select_1d1p_pick
from scripts.daily_top_proba_store import collect_hybrid_proba_picks, load_today_matches_for_daily_top_proba
from scripts.hyb_p75_p80_selection import best_1d1p_pick_from_hyb, select_hyb_p75_p80_all
from scripts.hybrid_pick_selection import hybrid_criteria_plain, select_hybrid_picks


def main() -> int:
    db = os.getenv("BETTINGHUD_DB") or "data/bettinghud.db"
    matches, _ = load_today_matches_for_daily_top_proba()
    hyb = collect_hybrid_proba_picks(matches, limit=None)
    picks = select_hybrid_picks(hyb, limit=None)
    best = best_1d1p_pick_from_hyb(picks)
    pick, day, pool, age = load_1d1p_today_pick(db_path=db)

    print("day:", day)
    print("criteria:", hybrid_criteria_plain(english=False))
    print("pool_candidates:", pool)
    print("hyb_count:", len(picks))
    if picks:
        print("hyb_top:", [(p.get("rank"), p.get("fav_player"), round(float(p.get("p_model_fav") or 0) * 100, 1)) for p in picks[:8]])
    if best:
        print("best_proba:", best.get("fav_player"), f"{float(best.get('p_model_fav') or 0)*100:.1f}%", best.get("selection_mode"))
    if pick:
        print("1d1p:", pick.get("fav_player"), f"{float(pick.get('p_model_fav') or 0)*100:.1f}%", pick.get("selection_mode"))
        if best and pick.get("fav_player") != best.get("fav_player"):
            print("WARN: 1D1P != best proba in hybrid list")
            return 1
    else:
        print("1d1p: none (empty day ok)")
    # sanity: select_1d1p_pick on same pool
    alt = select_1d1p_pick(hyb)
    if pick and alt and pick.get("fav_player") != alt.get("fav_player"):
        print("WARN: load_1d1p_today_pick != select_1d1p_pick")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
