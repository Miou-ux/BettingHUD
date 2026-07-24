#!/usr/bin/env python3
"""Compare picks today: legacy P77 vs HYB P75+P80-all."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.daily_top_proba_store import collect_hybrid_proba_picks, load_today_matches_for_daily_top_proba
from scripts.hyb_p75_p80_selection import select_hyb_p75_p80_all, select_p80_rel80_picks
from scripts.hybrid_pick_selection import select_hybrid_picks_legacy
from scripts.experiment_july_expert_kelly import select_expert_picks, EXPERT_SCOUT
from scripts.match_rank_quality import duplicate_model_prob_keys
from dataclasses import replace

P75_TIER = replace(replace(EXPERT_SCOUT, min_proba=0.73, max_picks=6), tier_fill=True)


def _fmt(row: dict) -> str:
    p = float(row.get("p_model_fav") or 0) * 100
    ev = float(row.get("ev_fav_pct") or 0)
    rel = row.get("data_reliability_score")
    seg = row.get("hybrid_segment") or row.get("hybrid_tier") or "?"
    fav = row.get("fav_player") or row.get("player1")
    return f"{fav} | p={p:.1f}% ev={ev:+.1f}% rel={rel} seg={seg}"


def main() -> int:
    matches, _ = load_today_matches_for_daily_top_proba()
    dup = duplicate_model_prob_keys(matches)
    pool = collect_hybrid_proba_picks.__wrapped__ if False else None  # noqa
    from scripts.daily_top_proba_store import collect_top5_proba_picks
    from scripts.hyb_p75_p80_selection import P80_MIN_REL

    raw = collect_top5_proba_picks(
        matches,
        limit=None,
        ev_min_frac=0.0,
        ev_max_frac=1.0,
        today_only=True,
        major_only=True,
        min_proba_frac=0.73,
        min_reliability_score=P80_MIN_REL,
    )

    legacy = select_hybrid_picks_legacy(raw, limit=None, duplicate_keys=dup, apply_telegram_proba_filter=False)
    hyb = select_hyb_p75_p80_all(raw, duplicate_keys=dup, limit=None)
    p75 = select_expert_picks(raw, duplicate_keys=dup, cfg=P75_TIER)
    p80 = select_p80_rel80_picks(raw, duplicate_keys=dup)
    p80_only = [p for p in p80 if _match_key(p) not in {_match_key(x) for x in p75}]

    print("=== 2026-07-24 · majors pool raw:", len(raw), "===")
    print(f"Legacy P77     : {len(legacy)} picks")
    for r in legacy:
        print("  ", _fmt(r))
    print(f"P75-TIER only  : {len(p75)} picks")
    for r in p75:
        print("  ", _fmt({**r, "hybrid_segment": "p75_tier"}))
    print(f"P80 add-ons    : {len(p80_only)} picks (not in P75-TIER)")
    for r in p80_only:
        print("  ", _fmt({**r, "hybrid_segment": "p80_addon"}))
    print(f"HYB P75+P80-all: {len(hyb)} picks")
    for r in hyb:
        print(f"  #{r.get('rank')} ", _fmt(r))

    added = []
    legacy_keys = {str(r.get("match_name") or "").lower() for r in legacy}
    for r in hyb:
        mk = str(r.get("match_name") or "").lower()
        if mk not in legacy_keys:
            added.append(r)
    print(f"\nDelta vs legacy P77: +{len(added)} pick(s)")
    for r in added:
        print("  NEW:", _fmt(r))
    return 0


def _match_key(row: dict) -> str:
    return str(row.get("match_name") or "").strip().lower()


if __name__ == "__main__":
    raise SystemExit(main())
