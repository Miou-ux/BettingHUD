#!/usr/bin/env python3
"""Backtest: tous les picks proba≥80% rel≥80, sans filtre EV."""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.backtest_pack12_global_2026 import BR_START, LIVE_CUTOFF, _perf_flat
from scripts.backtest_prod_top5_2026 import _kelly_sim, _tag
from scripts.backtest_scout_mega_grid import P75_TIER, _daily_pools_unlimited, _run_cfg, _summarize
from scripts.bets_db import open_db
from scripts.experiment_july_expert_kelly import _attach_settlement, _settlement_map, select_expert_picks
from scripts.match_rank_quality import duplicate_model_prob_keys, passes_public_pick_gates
from scripts.ml_model import TennisMLModel


def select_p80_rel80_all(
    pool: list[dict],
    *,
    duplicate_keys: set,
    min_proba: float,
    min_rel: int,
    min_ev_pct: float | None = None,
    max_ev_pct: float | None = None,
) -> list[dict]:
    from scripts.hybrid_pick_selection import ev_fav_pct

    out: list[dict] = []
    seen: set[str] = set()
    for row in sorted(pool, key=lambda r: -float(r.get("p_model_fav") or 0)):
        if float(row.get("p_model_fav") or 0) < min_proba:
            continue
        if not passes_public_pick_gates(row, duplicate_keys=duplicate_keys, min_score=min_rel):
            continue
        ev = ev_fav_pct(row)
        if min_ev_pct is not None and ev < min_ev_pct:
            continue
        if max_ev_pct is not None and ev > max_ev_pct:
            continue
        mk = str(row.get("match_name") or "").strip().lower()
        if not mk or mk in seen:
            continue
        seen.add(mk)
        out.append(dict(row))
    return out


def _run_p80(
    day_pools: dict[int, dict[str, tuple[list[dict], set]]],
    *,
    conn,
    smap: dict,
    min_proba: float,
    min_rel: int,
    min_ev_pct: float | None = None,
    max_ev_pct: float | None = None,
) -> list[dict]:
    picks: list[dict] = []
    for year, by_day in day_pools.items():
        for day, (pool, dup) in sorted(by_day.items()):
            for p in select_p80_rel80_all(
                pool,
                duplicate_keys=dup,
                min_proba=min_proba,
                min_rel=min_rel,
                min_ev_pct=min_ev_pct,
                max_ev_pct=max_ev_pct,
            ):
                row = dict(p)
                if year == 2026 and day >= LIVE_CUTOFF:
                    row = _attach_settlement(row, smap, conn=conn)
                picks.append(row)
    return picks


def _print_seg(name: str, m: dict) -> None:
    for seg in ("2025", "2026", "live"):
        s = m[seg]
        km = "ΣKellyM" if seg == "2025" else "Kelly"
        val = s["kelly_m"] if seg == "2025" else s["kelly"]
        print(
            f"  {seg:<5} {km} {val:+.1f}€ | hit {s['hit']:.1f}% | "
            f"settled {s['settled']}/{s['picks']} | flat {s['flat']:+.1f}u"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-proba", type=float, default=0.80, help="Proba fav min (frac, default 0.80)")
    ap.add_argument("--min-rel", type=int, default=80)
    ap.add_argument("--min-ev", type=float, default=None, help="EV fav min %% (e.g. 0)")
    ap.add_argument("--max-ev", type=float, default=None, help="EV fav max %%")
    ap.add_argument("--compare-all", action="store_true", help="Compare no EV vs EV≥0 vs P75-TIER")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    args = ap.parse_args()

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    smap = _settlement_map(args.db)

    pools = {2025: _daily_pools_unlimited(2025, ml), 2026: _daily_pools_unlimited(2026, ml)}
    day_pools = {
        y: {d: (p, duplicate_model_prob_keys(p)) for d, p in by.items()}
        for y, by in pools.items()
    }

    conn = open_db(args.db)
    try:
        if args.compare_all:
            variants = [
                ("P≥80 rel≥80 (no EV)", None, None),
                ("P≥80 rel≥80 EV≥0", 0.0, None),
                ("P75-TIER", "p75", None),
            ]
            results: list[tuple[str, dict]] = []
            for label, min_ev, max_ev in variants:
                if label == "P75-TIER":
                    picks = _run_cfg(P75_TIER, day_pools, conn=conn, smap=smap)
                else:
                    picks = _run_p80(
                        day_pools,
                        conn=conn,
                        smap=smap,
                        min_proba=args.min_proba,
                        min_rel=args.min_rel,
                        min_ev_pct=min_ev,
                        max_ev_pct=max_ev,
                    )
                results.append((label, _summarize(picks, ml)))
                daily_n = []
                for y, by in day_pools.items():
                    for day, (pool, dup) in by.items():
                        if label == "P75-TIER":
                            continue
                        n = len(
                            select_p80_rel80_all(
                                pool,
                                duplicate_keys=dup,
                                min_proba=args.min_proba,
                                min_rel=args.min_rel,
                                min_ev_pct=min_ev,
                                max_ev_pct=max_ev,
                            )
                        )
                        if n:
                            daily_n.append(n)
                avg = sum(daily_n) / max(1, len(daily_n)) if daily_n else 0.0
                print(f"=== {label} ===" + (f" (~{avg:.1f} pk/j)" if label != "P75-TIER" else ""))
                _print_seg(label, results[-1][1])
                print()
            print("=== Δ vs P75-TIER ===")
            ref = next(m for lb, m in results if lb == "P75-TIER")
            for label, m in results:
                if label == "P75-TIER":
                    continue
                print(label + ":")
                for seg, key in (("2025", "kelly_m"), ("2026", "kelly"), ("live", "kelly")):
                    print(f"  {seg}: {m[seg][key] - ref[seg][key]:+.1f}€")
            return 0

        p80_picks = _run_p80(
            day_pools,
            conn=conn,
            smap=smap,
            min_proba=args.min_proba,
            min_rel=args.min_rel,
            min_ev_pct=args.min_ev,
            max_ev_pct=args.max_ev,
        )
        p75_picks = _run_cfg(P75_TIER, day_pools, conn=conn, smap=smap)
    finally:
        conn.close()

    ev_label = ""
    if args.min_ev is not None:
        ev_label += f" EV≥{args.min_ev:.0f}%"
    if args.max_ev is not None:
        ev_label += f" EV≤{args.max_ev:.0f}%"

    daily_counts: list[int] = []
    for y, by in day_pools.items():
        for day, (pool, dup) in by.items():
            n = len(
                select_p80_rel80_all(
                    pool,
                    duplicate_keys=dup,
                    min_proba=args.min_proba,
                    min_rel=args.min_rel,
                    min_ev_pct=args.min_ev,
                    max_ev_pct=args.max_ev,
                )
            )
            if n:
                daily_counts.append(n)
    avg_picks = sum(daily_counts) / max(1, len(daily_counts))

    print(f"Stratégie P≥{args.min_proba*100:.0f}% rel≥{args.min_rel}{ev_label} — tous les picks")
    print(f"Pool illimité | settlement match_results (live) | DB: {args.db}")
    print(f"Picks moy: {avg_picks:.1f}/j\n")

    m_p80 = _summarize(p80_picks, ml)
    m_p75 = _summarize(p75_picks, ml)

    print("=== Stratégie ===")
    _print_seg("strat", m_p80)

    print("\n=== P75-TIER (référence) ===")
    _print_seg("p75", m_p75)

    print("\n=== Δ vs P75-TIER ===")
    for seg, key in (("2025", "kelly_m"), ("2026", "kelly"), ("live", "kelly")):
        d = m_p80[seg][key] - m_p75[seg][key]
        print(f"  {seg}: {d:+.1f}€")

    evs = [float(p.get("ev_fav_pct") or 0) for p in p80_picks if p.get("ev_fav_pct") is not None]
    if evs:
        evs_sorted = sorted(evs)
        print(
            f"\nEV fav (n={len(evs)}): min {evs_sorted[0]:.1f}% "
            f"med {evs_sorted[len(evs)//2]:.1f}% max {evs_sorted[-1]:.1f}%"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
