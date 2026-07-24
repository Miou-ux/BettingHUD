#!/usr/bin/env python3
"""Gains Kelly cumulés (composés) par année — HYBRID prod vs HYB P75+P80-all."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from collections import defaultdict

from scripts.backtest_hybrid_p75_p80 import _run_hybrid
from scripts.backtest_pack12_global_2026 import BR_START, LIVE_CUTOFF, _kelly_sim, _tag
from scripts.backtest_prod_top5_2026 import select_prod_top5_day
from scripts.backtest_scout_mega_grid import P75_TIER, _daily_pools_unlimited
from scripts.bets_db import open_db
from scripts.experiment_july_expert_kelly import _attach_settlement, _settlement_map
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.ml_model import TennisMLModel


def _run_prod_hybrid(day_pools, *, conn, smap):
    picks = []
    for year, by_day in sorted(day_pools.items()):
        for day in sorted(by_day):
            pool, dup = by_day[day]
            for p in select_prod_top5_day(pool):
                row = dict(p)
                if year == 2026 and day >= LIVE_CUTOFF:
                    row = _attach_settlement(row, smap, conn=conn)
                picks.append(row)
    return picks


def _seg_picks(picks: list[dict], seg: str) -> list[dict]:
    if seg == "2025":
        return [p for p in picks if str(p.get("calendar_date") or "")[:4] == "2025"]
    if seg == "2026":
        return [p for p in picks if str(p.get("calendar_date") or "")[:4] == "2026"]
    if seg == "live":
        return [p for p in picks if str(p.get("calendar_date") or "")[:10] >= LIVE_CUTOFF]
    return picks


def _kelly_monthly_sum(picks: list[dict], ml: TennisMLModel) -> dict:
    """2025 ref: Σ Kelly mensuel, reset 100€ chaque mois (évite explosion composée)."""
    by_m: dict[str, list[dict]] = defaultdict(list)
    for p in picks:
        by_m[str(p.get("calendar_date") or "")[:7]].append(p)
    profit = sum(
        float(_kelly_sim(_tag(c, "m"), ml, br_start=BR_START).get("net_profit_eur") or 0)
        for c in by_m.values()
        if c
    )
    settled = sum(1 for p in picks if p.get("settled"))
    wins = sum(1 for p in picks if p.get("settled") and p.get("won"))
    hit = (100.0 * wins / settled) if settled else 0.0
    return {"profit_eur": profit, "settled": settled, "picks": len(picks), "hit": hit, "months": len(by_m)}


def _kelly_cumul(picks: list[dict], ml: TennisMLModel, label: str) -> dict:
    k = _kelly_sim(_tag(picks, label), ml, br_start=BR_START)
    profit = float(k.get("net_profit_eur") or 0)
    final_br = float(k.get("bankroll_final") or BR_START)
    n = int(k.get("n_bets") or 0)
    settled = sum(1 for p in picks if p.get("settled"))
    wins = sum(1 for p in picks if p.get("settled") and p.get("won"))
    hit = (100.0 * wins / settled) if settled else 0.0
    mult = final_br / BR_START if BR_START > 0 else 1.0
    return {
        "profit_eur": profit,
        "final_br": final_br,
        "mult": mult,
        "n_bets": n,
        "settled": settled,
        "picks": len(picks),
        "hit": hit,
        "max_dd": float(k.get("max_drawdown_pct") or 0),
    }


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
        strategies = {
            "HYBRID prod": _run_prod_hybrid(day_pools, conn=conn, smap=smap),
            "HYB P75+P80-all": _run_hybrid(
                day_pools,
                base_cfg=P75_TIER,
                min_proba=0.80,
                min_rel=80,
                min_ev_pct=None,
                max_ev_pct=None,
                conn=conn,
                smap=smap,
            ),
        }
    finally:
        conn.close()

    segments = [
        ("2025", "2025"),
        ("2026", "2026"),
        ("Live replay", "live"),
    ]

    print(f"Kelly | BR départ {BR_START:.0f}€ | pool illimité | match_results live")
    print(f"2025 = ΣKellyM (reset mensuel) · 2026 & live = composé séquentiel\n")
    print(f"DB: {db}\n")

    # 2025 — ΣKellyM
    print("=== 2025 (ΣKellyM mensuel — reset 100€/mois) ===")
    print(f"{'Stratégie':<22} {'Gain €':>10} {'Réglés':>10} {'Hit':>6}")
    print("-" * 52)
    r25: dict[str, dict] = {}
    for name, all_picks in strategies.items():
        chunk = _seg_picks(all_picks, "2025")
        r = _kelly_monthly_sum(chunk, ml)
        r25[name] = r
        print(f"{name:<22} {r['profit_eur']:>+10.1f} {r['settled']:>4}/{r['picks']:<4} {r['hit']:>5.1f}%")
    print(f"  → Δ HYB vs prod: {r25['HYB P75+P80-all']['profit_eur'] - r25['HYBRID prod']['profit_eur']:+.1f}€\n")

    for seg_label, seg_key in [("2026 (Kelly composé)", "2026"), ("Live replay (Kelly composé)", "live")]:
        print(f"=== {seg_label} ===")
        print(f"{'Stratégie':<22} {'Gain €':>10} {'BR finale':>10} {'×BR':>6} {'Réglés':>8} {'Hit':>6} {'DD':>5}")
        print("-" * 72)
        for name, all_picks in strategies.items():
            chunk = _seg_picks(all_picks, seg_key)
            r = _kelly_cumul(chunk, ml, name[:8])
            print(
                f"{name:<22} {r['profit_eur']:>+10.1f} {r['final_br']:>10.1f} "
                f"{r['mult']:>5.2f}× {r['settled']:>4}/{r['picks']:<3} {r['hit']:>5.1f}% {r['max_dd']:>4.0f}%"
            )
        r_prod = _kelly_cumul(_seg_picks(strategies["HYBRID prod"], seg_key), ml, "hp")
        r_new = _kelly_cumul(_seg_picks(strategies["HYB P75+P80-all"], seg_key), ml, "hn")
        print(f"  → Δ HYB vs prod: {r_new['profit_eur'] - r_prod['profit_eur']:+.1f}€\n")

    print("=== Synthèse ===")
    for name in strategies:
        p25 = r25[name]["profit_eur"]
        p26 = _kelly_cumul(_seg_picks(strategies[name], "2026"), ml, "s")["profit_eur"]
        pl = _kelly_cumul(_seg_picks(strategies[name], "live"), ml, "l")["profit_eur"]
        print(
            f"  {name}:\n"
            f"    2025 ΣKellyM     {p25:>+10.1f}€\n"
            f"    2026 composé     {p26:>+10.1f}€  (100€ → {_kelly_cumul(_seg_picks(strategies[name], '2026'), ml, 'x')['final_br']:.1f}€)\n"
            f"    Live composé     {pl:>+10.1f}€  (100€ → {_kelly_cumul(_seg_picks(strategies[name], 'live'), ml, 'x')['final_br']:.1f}€)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
