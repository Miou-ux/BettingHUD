#!/usr/bin/env python3
"""Portfolio hybride : P75-TIER + picks P≥80 rel≥80 (complément)."""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.backtest_pack12_global_2026 import LIVE_CUTOFF
from scripts.backtest_p80_rel80_all import _run_p80, select_p80_rel80_all
from scripts.backtest_scout_mega_grid import P75_TIER, _daily_pools_unlimited, _run_cfg, _summarize
from scripts.bets_db import open_db
from scripts.experiment_july_expert_kelly import (
    ExpertCfg,
    _attach_settlement,
    _settlement_map,
    select_expert_picks,
)
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.ml_model import TennisMLModel


def _match_key(row: dict) -> str:
    return str(row.get("match_name") or "").strip().lower()


def _merge_picks(base: list[dict], extra: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in base + extra:
        mk = _match_key(row)
        if not mk or mk in seen:
            continue
        seen.add(mk)
        out.append(dict(row))
    return out


def _run_hybrid(
    day_pools: dict[int, dict[str, tuple[list[dict], set]]],
    *,
    base_cfg: ExpertCfg,
    min_proba: float,
    min_rel: int,
    min_ev_pct: float | None,
    max_ev_pct: float | None,
    conn,
    smap: dict,
) -> list[dict]:
    picks: list[dict] = []
    for year, by_day in sorted(day_pools.items()):
        for day in sorted(by_day):
            pool, dup = by_day[day]
            base = select_expert_picks(pool, duplicate_keys=dup, cfg=base_cfg)
            extra = select_p80_rel80_all(
                pool,
                duplicate_keys=dup,
                min_proba=min_proba,
                min_rel=min_rel,
                min_ev_pct=min_ev_pct,
                max_ev_pct=max_ev_pct,
            )
            merged = _merge_picks(base, extra)
            for p in merged:
                row = dict(p)
                row = _attach_settlement(row, smap, conn=conn)
                picks.append(row)
    return picks


def _avg_picks_day(day_pools, fn) -> float:
    counts: list[int] = []
    for y, by in day_pools.items():
        for day, (pool, dup) in by.items():
            n = fn(y, day, pool, dup)
            if n:
                counts.append(n)
    return sum(counts) / max(1, len(counts))


def main() -> int:
    ap = argparse.ArgumentParser()
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

    hybrids = [
        ("P75-TIER", None),
        ("P80-R80-EV15+", dict(min_proba=0.80, min_rel=80, min_ev_pct=15.0, max_ev_pct=None)),
        ("P80-R80-EV15-55", dict(min_proba=0.80, min_rel=80, min_ev_pct=15.0, max_ev_pct=55.0)),
        ("P80-R80-all", dict(min_proba=0.80, min_rel=80, min_ev_pct=None, max_ev_pct=None)),
        (
            "HYB P75+P80-EV15+",
            dict(min_proba=0.80, min_rel=80, min_ev_pct=15.0, max_ev_pct=None, hybrid=True),
        ),
        (
            "HYB P75+P80-EV15-55",
            dict(min_proba=0.80, min_rel=80, min_ev_pct=15.0, max_ev_pct=55.0, hybrid=True),
        ),
        (
            "HYB P75+P80-all",
            dict(min_proba=0.80, min_rel=80, min_ev_pct=None, max_ev_pct=None, hybrid=True),
        ),
    ]

    conn = open_db(args.db)
    results: list[tuple[str, dict, float]] = []
    try:
        for name, opts in hybrids:
            if opts is None:
                picks = _run_cfg(P75_TIER, day_pools, conn=conn, smap=smap)
                avg = _avg_picks_day(
                    day_pools,
                    lambda y, d, pool, dup: len(select_expert_picks(pool, duplicate_keys=dup, cfg=P75_TIER)),
                )
            elif opts.get("hybrid"):
                kw = {k: v for k, v in opts.items() if k != "hybrid"}
                picks = _run_hybrid(day_pools, base_cfg=P75_TIER, conn=conn, smap=smap, **kw)

                def _count_hybrid(y, d, pool, dup, kw=kw):
                    b = select_expert_picks(pool, duplicate_keys=dup, cfg=P75_TIER)
                    e = select_p80_rel80_all(pool, duplicate_keys=dup, **kw)
                    return len(_merge_picks(b, e))

                avg = _avg_picks_day(day_pools, _count_hybrid)
            else:
                picks = _run_p80(day_pools, conn=conn, smap=smap, **opts)
                avg = _avg_picks_day(
                    day_pools,
                    lambda y, d, pool, dup, kw=opts: len(
                        select_p80_rel80_all(pool, duplicate_keys=dup, **kw)
                    ),
                )
            results.append((name, _summarize(picks, ml), avg))
    finally:
        conn.close()

    ref = next(m for n, m, _ in results if n == "P75-TIER")

    print(f"Portfolio hybride P75-TIER + P≥80 | pool illimité | prod DB\n")
    print(f"{'Stratégie':<24} {'pk/j':>4} {'2025ΣKM':>8} {'2026K':>8} {'liveK':>7} {'hitL':>5} {'Σ3':>8}")
    print("-" * 68)

    def _score(m: dict) -> float:
        return m["2025"]["kelly_m"] + m["2026"]["kelly"] + m["live"]["kelly"]

    def _triple(m: dict) -> bool:
        p80_all = next(m2 for n, m2, _ in results if n == "P80-R80-all")
        p75 = ref
        return (
            m["2025"]["kelly_m"] > p75["2025"]["kelly_m"] + 0.5
            and m["2026"]["kelly"] > p75["2026"]["kelly"] + 0.5
            and m["live"]["kelly"] > p75["live"]["kelly"] + 0.5
        )

    for name, m, avg in sorted(results, key=lambda t: -_score(t[1])):
        print(
            f"{name:<24} {avg:>4.1f} {m['2025']['kelly_m']:>+8.0f} {m['2026']['kelly']:>+8.0f} "
            f"{m['live']['kelly']:>+7.1f} {m['live']['hit']:>4.0f}% {_score(m):>+8.0f}"
        )

    print(f"\n=== Δ hybrides vs P75-TIER seul ===")
    for name, m, avg in results:
        if not name.startswith("HYB"):
            continue
        d25 = m["2025"]["kelly_m"] - ref["2025"]["kelly_m"]
        d26 = m["2026"]["kelly"] - ref["2026"]["kelly"]
        dl = m["live"]["kelly"] - ref["live"]["kelly"]
        extra = avg - next(a for n, _, a in results if n == "P75-TIER")
        print(
            f"  {name}: Δ25 {d25:+.0f} Δ26 {d26:+.0f} Δlive {dl:+.1f}€ | "
            f"+{extra:.1f} pk/j vs P75 seul | hit live {m['live']['hit']:.0f}%"
        )

    best_hyb = max((t for t in results if t[0].startswith("HYB")), key=lambda t: _score(t[1]))
    p80_all = next(t for t in results if t[0] == "P80-R80-all")
    print(f"\n=== Meilleur hybride: {best_hyb[0]} ===")
    print(f"  Σ3 {_score(best_hyb[1]):+.0f}€ vs P75 {_score(ref):+.0f}€ vs P80-all {_score(p80_all[1]):+.0f}€")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
