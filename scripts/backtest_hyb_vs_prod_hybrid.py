#!/usr/bin/env python3
"""HYB P75+P80-all vs hybride prod actuel (select_hybrid_picks)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.backtest_hybrid_p75_p80 import _merge_picks, _run_hybrid
from scripts.backtest_pack12_global_2026 import LIVE_CUTOFF
from scripts.backtest_p80_rel80_all import select_p80_rel80_all
from scripts.backtest_prod_top5_2026 import select_prod_top5_day
from scripts.backtest_scout_mega_grid import P75_TIER, _daily_pools_unlimited, _summarize
from scripts.bets_db import open_db
from scripts.experiment_july_expert_kelly import (
    _attach_settlement,
    _settlement_map,
    select_expert_picks,
)
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.ml_model import TennisMLModel


def _run_prod_hybrid(
    day_pools: dict[int, dict[str, tuple[list[dict], set]]],
    *,
    conn,
    smap: dict,
) -> list[dict]:
    picks: list[dict] = []
    for year, by_day in sorted(day_pools.items()):
        for day in sorted(by_day):
            pool, dup = by_day[day]
            for p in select_prod_top5_day(pool):
                row = dict(p)
                if year == 2026 and day >= LIVE_CUTOFF:
                    row = _attach_settlement(row, smap, conn=conn)
                picks.append(row)
    return picks


def _avg_day(day_pools, fn) -> float:
    ns: list[int] = []
    for y, by in day_pools.items():
        for day, (pool, dup) in by.items():
            n = fn(pool, dup)
            if n:
                ns.append(n)
    return sum(ns) / max(1, len(ns))


def main() -> int:
    db = os.path.join(ROOT, "data", "bettinghud.db")
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    smap = _settlement_map(db)

    pools = {2025: _daily_pools_unlimited(2025, ml), 2026: _daily_pools_unlimited(2026, ml)}
    day_pools = {
        y: {d: (p, duplicate_model_prob_keys(p)) for d, p in by.items()}
        for y, by in pools.items()
    }

    conn = open_db(db)
    try:
        hyb_new = _run_hybrid(
            day_pools,
            base_cfg=P75_TIER,
            min_proba=0.80,
            min_rel=80,
            min_ev_pct=None,
            max_ev_pct=None,
            conn=conn,
            smap=smap,
        )
        hyb_prod = _run_prod_hybrid(day_pools, conn=conn, smap=smap)
    finally:
        conn.close()

    m_new = _summarize(hyb_new, ml)
    m_prod = _summarize(hyb_prod, ml)

    avg_new = _avg_day(
        day_pools,
        lambda pool, dup: len(
            _merge_picks(
                select_expert_picks(pool, duplicate_keys=dup, cfg=P75_TIER),
                select_p80_rel80_all(pool, duplicate_keys=dup, min_proba=0.80, min_rel=80),
            )
        ),
    )
    avg_prod = _avg_day(day_pools, lambda pool, dup: len(select_prod_top5_day(pool)))

    print("Comparaison Kelly | pool illimité | settlement match_results (live)")
    print(f"DB: {db}\n")

    rows = [
        ("HYBRID prod (actuel)", m_prod, avg_prod),
        ("HYB P75+P80-all", m_new, avg_new),
    ]

    print(f"{'Stratégie':<24} {'pk/j':>4}  {'2025 ΣKellyM':>12}  {'2026 Kelly':>11}  {'Live Kelly':>11}  {'hit L':>6}")
    print("-" * 78)
    for name, m, avg in rows:
        print(
            f"{name:<24} {avg:>4.1f}  {m['2025']['kelly_m']:>+11.0f}€  "
            f"{m['2026']['kelly']:>+10.0f}€  {m['live']['kelly']:>+10.1f}€  "
            f"{m['live']['hit']:>5.1f}%"
        )
        print(
            f"{'':24}       settled {m['2025']['settled']}/{m['2025']['picks']} | "
            f"{m['2026']['settled']}/{m['2026']['picks']} | "
            f"{m['live']['settled']}/{m['live']['picks']}"
        )

    print(f"\n=== Δ HYB P75+P80-all vs HYBRID prod ===")
    for seg, key, label in (
        ("2025", "kelly_m", "2025 ΣKellyM"),
        ("2026", "kelly", "2026 Kelly"),
        ("live", "kelly", "Live Kelly"),
    ):
        d = m_new[seg][key] - m_prod[seg][key]
        pct = (100.0 * d / m_prod[seg][key]) if m_prod[seg][key] else 0.0
        print(f"  {label}: {d:+.1f}€ ({pct:+.1f}%)")

    for seg, label in (("2025", "2025"), ("2026", "2026"), ("live", "live")):
        dh = m_new[seg]["hit"] - m_prod[seg]["hit"]
        print(f"  Hit {label}: {dh:+.1f} pt")

    s_new = m_new["2025"]["kelly_m"] + m_new["2026"]["kelly"] + m_new["live"]["kelly"]
    s_prod = m_prod["2025"]["kelly_m"] + m_prod["2026"]["kelly"] + m_prod["live"]["kelly"]
    print(f"\n  Σ combiné (2025+2026+live): {s_new:+.0f}€ vs {s_prod:+.0f}€ → Δ {s_new - s_prod:+.0f}€")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
