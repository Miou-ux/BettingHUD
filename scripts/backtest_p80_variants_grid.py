#!/usr/bin/env python3
"""Grille variantes P≥80 + rel≥80 + bandes EV — vs P75-TIER."""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.backtest_scout_mega_grid import P75_TIER, _daily_pools_unlimited, _run_cfg, _summarize
from scripts.backtest_p80_rel80_all import _run_p80, select_p80_rel80_all
from scripts.bets_db import open_db
from scripts.experiment_july_expert_kelly import _settlement_map
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.ml_model import TennisMLModel


@dataclass(frozen=True)
class P80Variant:
    name: str
    min_proba: float = 0.80
    min_rel: int = 80
    min_ev_pct: float | None = None
    max_ev_pct: float | None = None


VARIANTS: list[P80Variant] = [
    P80Variant("P80-R80-all"),
    P80Variant("P80-R80-EV0+", min_ev_pct=0.0),
    P80Variant("P80-R80-EV6+", min_ev_pct=6.0),
    P80Variant("P80-R80-EV6-55", min_ev_pct=6.0, max_ev_pct=55.0),
    P80Variant("P80-R80-EV6-45", min_ev_pct=6.0, max_ev_pct=45.0),
    P80Variant("P80-R80-EV15-35", min_ev_pct=15.0, max_ev_pct=35.0),
    P80Variant("P80-R80-EV15-55", min_ev_pct=15.0, max_ev_pct=55.0),
    P80Variant("P80-R80-EV0-55", min_ev_pct=0.0, max_ev_pct=55.0),
    P80Variant("P80-R80-EV10-40", min_ev_pct=10.0, max_ev_pct=40.0),
    P80Variant("P77-R80-EV6-55", min_proba=0.77, min_ev_pct=6.0, max_ev_pct=55.0),
    P80Variant("P78-R80-EV6-55", min_proba=0.78, min_ev_pct=6.0, max_ev_pct=55.0),
    P80Variant("P79-R80-EV6-55", min_proba=0.79, min_ev_pct=6.0, max_ev_pct=55.0),
    P80Variant("P81-R80-EV6-55", min_proba=0.81, min_ev_pct=6.0, max_ev_pct=55.0),
    P80Variant("P80-R82-EV6-55", min_rel=82, min_ev_pct=6.0, max_ev_pct=55.0),
    P80Variant("P80-R85-EV6-55", min_rel=85, min_ev_pct=6.0, max_ev_pct=55.0),
    P80Variant("P80-R80-EV6-55-G30", min_ev_pct=6.0, max_ev_pct=55.0),  # gap filter added below
]


def _avg_picks_day(day_pools, v: P80Variant) -> float:
    counts: list[int] = []
    for _y, by in day_pools.items():
        for _day, (pool, dup) in by.items():
            n = len(
                select_p80_rel80_all(
                    pool,
                    duplicate_keys=dup,
                    min_proba=v.min_proba,
                    min_rel=v.min_rel,
                    min_ev_pct=v.min_ev_pct,
                    max_ev_pct=v.max_ev_pct,
                )
            )
            if n:
                counts.append(n)
    return sum(counts) / max(1, len(counts))


def _run_variant(day_pools, v: P80Variant, *, conn, smap) -> list[dict]:
    picks = _run_p80(
        day_pools,
        conn=conn,
        smap=smap,
        min_proba=v.min_proba,
        min_rel=v.min_rel,
        min_ev_pct=v.min_ev_pct,
        max_ev_pct=v.max_ev_pct,
    )
    if v.name.endswith("-G30"):
        out: list[dict] = []
        for p in picks:
            try:
                gap = float(p.get("book_gap_pp") or 99)
            except (TypeError, ValueError):
                gap = 99.0
            if gap <= 30.0:
                out.append(p)
        return out
    return picks


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

    conn = open_db(args.db)
    results: list[tuple[P80Variant, dict, float]] = []
    try:
        ref_m = _summarize(_run_cfg(P75_TIER, day_pools, conn=conn, smap=smap), ml)
        for v in VARIANTS:
            m = _summarize(_run_variant(day_pools, v, conn=conn, smap=smap), ml)
            avg = _avg_picks_day(day_pools, v)
            results.append((v, m, avg))
    finally:
        conn.close()

    print(f"Grille P80+ variants | pool illimité | match_results live | DB: {args.db}\n")
    print("=== P75-TIER (réf) ===")
    print(
        f"  2025 ΣKM {ref_m['2025']['kelly_m']:+.0f}€ | 2026 {ref_m['2026']['kelly']:+.0f}€ | "
        f"live {ref_m['live']['kelly']:+.1f}€ | hit live {ref_m['live']['hit']:.0f}%"
    )

    def _score(m: dict) -> float:
        return m["2025"]["kelly_m"] + m["2026"]["kelly"] + m["live"]["kelly"]

    def _triple(m: dict) -> bool:
        return (
            m["2025"]["kelly_m"] > ref_m["2025"]["kelly_m"] + 0.5
            and m["2026"]["kelly"] > ref_m["2026"]["kelly"] + 0.5
            and m["live"]["kelly"] > ref_m["live"]["kelly"] + 0.5
        )

    print(f"\n{'Variante':<22} {'pk/j':>4} {'2025ΣKM':>8} {'2026K':>8} {'liveK':>7} {'hitL':>5} {'Σ3':>8} {'3x':>3}")
    print("-" * 72)
    ranked = sorted(results, key=lambda t: -_score(t[1]))
    for v, m, avg in ranked:
        s3 = _score(m)
        tx = "✓" if _triple(m) else ""
        print(
            f"{v.name:<22} {avg:>4.1f} {m['2025']['kelly_m']:>+8.0f} {m['2026']['kelly']:>+8.0f} "
            f"{m['live']['kelly']:>+7.1f} {m['live']['hit']:>4.0f}% {s3:>+8.0f} {tx:>3}"
        )

    triples = [(v, m) for v, m, _ in results if _triple(m)]
    print(f"\n=== Triple beat P75-TIER: {len(triples)} variante(s) ===")
    for v, m in sorted(triples, key=lambda t: -_score(t[1])):
        print(
            f"  {v.name}: 2025 {m['2025']['kelly_m']:+.0f}€ | 2026 {m['2026']['kelly']:+.0f}€ | "
            f"live {m['live']['kelly']:+.1f}€ | hit live {m['live']['hit']:.0f}% | "
            f"settled {m['live']['settled']}/{m['live']['picks']}"
        )

    print("\n=== Top 5 combiné (Σ2025+2026+live) ===")
    for v, m, avg in ranked[:5]:
        d25 = m["2025"]["kelly_m"] - ref_m["2025"]["kelly_m"]
        d26 = m["2026"]["kelly"] - ref_m["2026"]["kelly"]
        dl = m["live"]["kelly"] - ref_m["live"]["kelly"]
        print(
            f"  {v.name} (~{avg:.1f}/j): Δ25 {d25:+.0f} Δ26 {d26:+.0f} Δlive {dl:+.1f}€ | "
            f"hit live {m['live']['hit']:.0f}%"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
