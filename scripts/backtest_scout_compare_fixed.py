#!/usr/bin/env python3
"""Compare SCOUT variants — settlement via match_results (correct live replay)."""
from __future__ import annotations

import argparse
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.backtest_scout_mega_grid import (  # noqa: E402
    P75_TIER,
    _daily_pools_unlimited,
    _gen_configs,
    _run_cfg,
    _summarize,
)
from scripts.bets_db import open_db
from scripts.experiment_july_expert_kelly import _settlement_map
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.ml_model import TennisMLModel


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="P75-TIER,v06996", help="Comma-separated variant labels")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    args = ap.parse_args()

    wanted = [x.strip() for x in args.labels.split(",") if x.strip()]
    all_cfgs = dict(_gen_configs(10000, random.Random(args.seed)))
    missing = [w for w in wanted if w not in all_cfgs]
    if missing:
        print(f"Unknown labels: {missing}")
        return 1

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    smap = _settlement_map(args.db)

    pools = {2025: _daily_pools_unlimited(2025, ml), 2026: _daily_pools_unlimited(2026, ml)}
    day_pools = {
        year: {d: (p, duplicate_model_prob_keys(p)) for d, p in by_day.items()}
        for year, by_day in pools.items()
    }

    print(f"Compare (match_results settlement) | DB: {args.db}\n")
    rows: list[tuple[str, dict]] = []
    conn = open_db(args.db)
    try:
        for label in wanted:
            cfg = all_cfgs[label]
            m = _summarize(_run_cfg(cfg, day_pools, conn=conn, smap=smap), ml)
            rows.append((label, m))
            print(f"=== {label} | {cfg.label} ===")
            for seg in ("2025", "2026", "live"):
                s = m[seg]
                km = "ΣKellyM" if seg == "2025" else "Kelly"
                print(
                    f"  {seg:<5} {km} {s['kelly' if seg != '2025' else 'kelly_m']:+.1f}€ | "
                    f"hit {s['hit']:.1f}% | settled {s['settled']}/{s['picks']}"
                )
            print()
    finally:
        conn.close()

    if len(rows) == 2 and rows[0][0] == "P75-TIER":
        ref = rows[0][1]
        _, alt = rows[1]
        label = wanted[1]
        print(f"=== {label} vs P75-TIER ===")
        for seg, metric in (("2025", "kelly_m"), ("2026", "kelly"), ("live", "kelly")):
            d = alt[seg][metric] - ref[seg][metric]
            win = "✓" if d > 0.5 else "✗"
            print(f"  {seg}: Δ{d:+.1f}€ {win}")
        triple = all(
            alt[s][("kelly_m" if s == "2025" else "kelly")] > ref[s][("kelly_m" if s == "2025" else "kelly")] + 0.5
            for s in ("2025", "2026", "live")
        )
        print(f"  Triple beat: {'OUI' if triple else 'NON'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
