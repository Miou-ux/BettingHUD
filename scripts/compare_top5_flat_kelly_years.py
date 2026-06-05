#!/usr/bin/env python3
"""Top 5 vs Top 5 p>=65% — mise fixe 5 EUR + Kelly par annee (backtest CSV)."""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import load_and_filter_bets_csv, resolve_backtest_csv, simulate_sequential_intraday
from scripts.ml_model import TennisMLModel
from scripts.simulate_top10_proba_2026 import (
    DEFAULT_EXTRA_EXCLUDE,
    DEFAULT_TOURNEY_LEVELS,
    KELLY_BASE,
    MAX_STAKE_PCT,
    _segment_calibration_key,
    flat_stake_metrics,
    select_top_proba_per_day,
)

BR_START = 100.0
STAKE = 5.0
EV_MIN = 15.0
EV_MAX = 100.0
TOP_N = 5


def _pool(csv_path: str, year: int, p_min_frac: float) -> pd.DataFrame:
    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    df = load_and_filter_bets_csv(
        csv_path,
        year=year,
        ev_min_pct=EV_MIN,
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=list(DEFAULT_TOURNEY_LEVELS),
        extra_tournament_tokens=extra,
    )
    df = df[df["ev"].astype(float) <= EV_MAX / 100.0].reset_index(drop=True)
    if p_min_frac > 0:
        df = df[df["p_model"].astype(float) >= p_min_frac].copy()
    return select_top_proba_per_day(df, top_n=TOP_N)


def _flat(sel: pd.DataFrame) -> dict:
    if sel.empty:
        return {"n": 0, "days": 0, "hit_pct": 0.0, "profit_eur": 0.0, "roi_pct": 0.0, "br_final": BR_START}
    rets = np.where(sel["won"].astype(bool), sel["odd"].astype(float) - 1.0, -1.0)
    n = len(sel)
    profit = float(rets.sum() * STAKE)
    return {
        "n": n,
        "days": int(sel["date"].nunique()),
        "hit_pct": float(sel["won"].mean() * 100),
        "profit_eur": profit,
        "roi_pct": profit / (n * STAKE) * 100,
        "br_final": BR_START + profit,
    }


def _kelly(sel: pd.DataFrame, ml: TennisMLModel) -> dict:
    if sel.empty:
        return {
            "profit_eur": 0.0,
            "br_final": BR_START,
            "roi_vol_pct": 0.0,
            "max_dd_pct": 0.0,
            "sharpe_j": 0.0,
            "pf": 0.0,
            "volume_eur": 0.0,
        }
    work = sel.copy()
    work["segment_calibration_key"] = work.apply(
        lambda r: _segment_calibration_key(r, ml), axis=1
    )
    work = work.sort_values(["date", "p_model"], ascending=[True, False], kind="mergesort")
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    k = simulate_sequential_intraday(
        work,
        bankroll_start=BR_START,
        kelly_multiplier=1.0,
        max_stake_pct=MAX_STAKE_PCT,
        daily_stake_budget_pct=100.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=KELLY_BASE,
        segment_brier_scores=seg,
        global_brier_score=glob_b,
        stake_cap_basis="liquid",
    )
    return {
        "profit_eur": float(k["net_profit_eur"]),
        "br_final": float(k["bankroll_final"]),
        "roi_vol_pct": float(k["roi_on_staked_pct"]),
        "max_dd_pct": float(k["max_drawdown_pct"]),
        "sharpe_j": float(k["sharpe_daily"]),
        "pf": float(k.get("profit_factor") or 0.0),
        "volume_eur": float(k.get("total_staked_eur") or 0),
    }


def run_year(year: int, ml: TennisMLModel) -> None:
    csv_path = resolve_backtest_csv(ROOT, year)
    if not csv_path:
        print(f"\n[{year}] CSV introuvable — skip")
        return
    print("\n" + "=" * 76)
    print(f"BACKTEST {year} no-leak — {os.path.relpath(csv_path, ROOT)}")
    print(f"Top {TOP_N}/jour · EV {EV_MIN:.0f}-{EV_MAX:.0f}% · ATP/WTA G/M/A")
    print(
        f"Mise fixe {STAKE:.0f} EUR | Kelly 1/2 x Brier cap {MAX_STAKE_PCT:.0f}% "
        f"liq. BR {BR_START:.0f} EUR"
    )
    print("-" * 76)
    rows = []
    for label, pmin in [("Top 5", 0.0), ("Top 5 · proba >= 65%", 0.65)]:
        sel = _pool(csv_path, year, pmin)
        f = _flat(sel)
        k = _kelly(sel, ml)
        rows.append((label, f, k))
        print(f"\n{label}")
        print(
            f"  Paris {f['n']} · {f['days']} j · hit {f['hit_pct']:.1f}%"
        )
        print(
            f"  Fixe 5 EUR : PnL {f['profit_eur']:+.2f} EUR · ROI {f['roi_pct']:+.1f}% · "
            f"BR -> {f['br_final']:.2f} EUR"
        )
        print(
            f"  Kelly      : PnL {k['profit_eur']:+.2f} EUR · BR -> {k['br_final']:.2f} EUR "
            f"(indicatif si >>1000 EUR)"
        )
        print(
            f"               ROI vol {k['roi_vol_pct']:+.1f}% · DD {k['max_dd_pct']:.1f}% · "
            f"Sharpe {k['sharpe_j']:.2f} · PF {k['pf']:.2f}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2024,2025", help="Annees comma-sep")
    args = ap.parse_args()
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    for y in [int(x.strip()) for x in args.years.split(",") if x.strip()]:
        run_year(y, ml)


if __name__ == "__main__":
    main()
