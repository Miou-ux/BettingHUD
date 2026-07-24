#!/usr/bin/env python3
"""Perf HYB P75+P80-all depuis une date (défaut 2026-06-01)."""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.backtest_hybrid_p75_p80 import _run_hybrid
from scripts.backtest_pack12_global_2026 import BR_START, LIVE_CUTOFF, _perf_flat
from scripts.backtest_prod_top5_2026 import _kelly_sim, _tag
from scripts.backtest_scout_mega_grid import P75_TIER, _daily_pools_unlimited
from scripts.bets_db import open_db
from scripts.experiment_july_expert_kelly import _settlement_map
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.ml_model import TennisMLModel


def _filter_from(picks: list[dict], start: str) -> list[dict]:
    return [p for p in picks if str(p.get("calendar_date") or "")[:10] >= start]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2026-06-01")
    ap.add_argument("--to", dest="end", default="")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    args = ap.parse_args()

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    smap = _settlement_map(args.db)

    pools = {2026: _daily_pools_unlimited(2026, ml)}
    day_pools = {
        y: {
            d: (p, duplicate_model_prob_keys(p))
            for d, p in by.items()
            if d >= args.start and (not args.end or d <= args.end)
        }
        for y, by in pools.items()
    }

    conn = open_db(args.db)
    try:
        picks = _run_hybrid(
            day_pools,
            base_cfg=P75_TIER,
            min_proba=0.80,
            min_rel=80,
            min_ev_pct=None,
            max_ev_pct=None,
            conn=conn,
            smap=smap,
        )
    finally:
        conn.close()

    picks = _filter_from(picks, args.start)
    if args.end:
        picks = [p for p in picks if str(p.get("calendar_date") or "")[:10] <= args.end]

    n, ns, wins, flat = _perf_flat(picks)
    hit = (100.0 * wins / ns) if ns else 0.0
    kelly = _kelly_sim(_tag(picks, "hyb"), ml, br_start=BR_START)
    profit = float(kelly.get("net_profit_eur") or 0)
    final_br = float(kelly.get("bankroll_final") or BR_START)
    unsettled = n - ns

    by_m: dict[str, list[dict]] = defaultdict(list)
    for p in picks:
        by_m[str(p.get("calendar_date") or "")[:7]].append(p)

    print(f"HYB P75+P80-all | {args.start}" + (f" → {args.end}" if args.end else " → today"))
    print(f"Pool illimité · Kelly 0.65×Brier · BR départ {BR_START:.0f}€ · settlement match_results si live")
    print()
    print(f"Picks total     : {n}")
    print(f"Settled         : {ns} ({unsettled} en cours)")
    print(f"Hit rate        : {hit:.1f}% ({wins}G / {ns - wins}P)")
    print(f"Flat 1€/pick    : {flat:+.1f}€")
    print(f"Kelly composé   : {profit:+.1f}€  (BR finale {final_br:.1f}€, ×{final_br/BR_START:.2f})")
    print(f"Max drawdown    : {float(kelly.get('max_drawdown_pct') or 0):.1f}%")
    print(f"Jours actifs    : {len({str(p.get('calendar_date') or '')[:10] for p in picks})}")
    print(f"Moy picks/jour  : {n / max(1, len({str(p.get('calendar_date') or '')[:10] for p in picks})):.2f}")

    print("\n--- Par mois (Kelly reset 100€/mois) ---")
    for month in sorted(by_m):
        chunk = by_m[month]
        km = float(_kelly_sim(_tag(chunk, month), ml, br_start=BR_START).get("net_profit_eur") or 0)
        _, m_ns, m_w, m_flat = _perf_flat(chunk)
        m_hit = (100.0 * m_w / m_ns) if m_ns else 0.0
        print(f"  {month}: {len(chunk):>3} picks | hit {m_hit:5.1f}% | flat {m_flat:+6.0f}€ | ΣKellyM {km:+7.0f}€")

    live = [p for p in picks if str(p.get("calendar_date") or "")[:10] >= LIVE_CUTOFF]
    if live:
        lk = _kelly_sim(_tag(live, "live"), ml, br_start=BR_START)
        _, l_ns, l_w, l_flat = _perf_flat(live)
        l_hit = (100.0 * l_w / l_ns) if l_ns else 0.0
        print(f"\n--- Segment live (≥ {LIVE_CUTOFF}) ---")
        print(f"  {len(live)} picks | hit {l_hit:.1f}% | flat {l_flat:+.0f}€ | Kelly {float(lk.get('net_profit_eur') or 0):+.1f}€")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
