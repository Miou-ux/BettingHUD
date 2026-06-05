#!/usr/bin/env python3
"""Top 5 vs Top 5 p>=65% — mise Kelly live (PROD) + backtest 2026."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import load_and_filter_bets_csv, simulate_sequential_intraday
from scripts.bets_db import sync_algo_opportunities_from_results, sync_daily_top_proba_from_results
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key
from scripts.replay_live_top5_proba import (
    apply_ev_band,
    compute_metrics,
    dedupe_opportunities,
    load_algo_opportunities,
    select_top_proba_per_day,
)
from scripts.simulate_top10_proba_2026 import (
    DEFAULT_EXTRA_EXCLUDE,
    DEFAULT_TOURNEY_LEVELS,
    KELLY_BASE,
    MAX_STAKE_PCT,
    _segment_calibration_key,
    select_top_proba_per_day as select_top_proba_bt,
)

BR_START = 100.0
EV_MIN = 15.0
EV_MAX = 100.0
TOP_N = 5


def _select_live(
    db_path: str,
    start: str,
    *,
    p_min_frac: float = 0.0,
    top_n: int = TOP_N,
) -> pd.DataFrame:
    cn = sqlite3.connect(db_path)
    sync_algo_opportunities_from_results(cn)
    sync_daily_top_proba_from_results(cn)
    cn.close()
    raw = load_algo_opportunities(db_path=db_path, start=start)
    pool = apply_ev_band(dedupe_opportunities(raw))
    if p_min_frac > 0:
        pool = pool[pool["p_model"].astype(float) >= p_min_frac].copy()
    return select_top_proba_per_day(pool, top_n=top_n)


def _select_backtest(
    csv_path: str,
    *,
    p_min_frac: float = 0.0,
    top_n: int = TOP_N,
) -> pd.DataFrame:
    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    df = load_and_filter_bets_csv(
        csv_path,
        year=2026,
        ev_min_pct=EV_MIN,
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=list(DEFAULT_TOURNEY_LEVELS),
        extra_tournament_tokens=extra,
    )
    df = df[df["ev"].astype(float) <= EV_MAX / 100.0].reset_index(drop=True)
    if p_min_frac > 0:
        df = df[df["p_model"].astype(float) >= p_min_frac].copy()
    sel = select_top_proba_bt(df, top_n=top_n)
    sel = sel.copy()
    sel["settled"] = True
    return sel


def _kelly_row(df: pd.DataFrame, *, ml: TennisMLModel, label: str) -> dict:
    open_n = int((df.get("st") == "open").sum()) if "st" in df.columns else 0
    settled = df[df["settled"]].copy() if "settled" in df.columns else df.copy()
    if settled.empty:
        return {"strategie": label, "paris": len(df), "settled": 0, "open": open_n}

    m = compute_metrics(settled, ml=ml, label=label, br_start=BR_START)
    work = settled.sort_values(["date", "p_model"], ascending=[True, False], kind="mergesort")
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    kelly = simulate_sequential_intraday(
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
        "strategie": label,
        "paris": len(df),
        "settled": len(settled),
        "open": open_n,
        "jours": m["jours"],
        "hit_pct": m["hit_pct"],
        "kelly_profit_eur": float(kelly["net_profit_eur"]),
        "kelly_br_final": float(kelly["bankroll_final"]),
        "kelly_roi_vol_pct": float(kelly["roi_on_staked_pct"]),
        "kelly_max_dd_pct": float(kelly["max_drawdown_pct"]),
        "kelly_sharpe_j": float(kelly["sharpe_daily"]),
        "kelly_pf": float(kelly.get("profit_factor") or 0.0),
        "volume_mise_eur": float(kelly.get("total_staked_eur") or kelly.get("staked_eur") or 0),
    }


def _print_block(title: str, rows: list[dict], *, note: str = "") -> None:
    print("=" * 72)
    print(title)
    if note:
        print(note)
    print(
        f"Kelly : 1/2 x Brier segment, cap {MAX_STAKE_PCT:.0f}% liquidite, "
        f"BR depart {BR_START:.0f} EUR"
    )
    print("-" * 72)
    for r in rows:
        if r.get("settled", 0) == 0:
            print(f"{r['strategie']}: aucun pari regle")
            continue
        print(
            f"{r['strategie']}\n"
            f"  Paris {r['paris']} (regles {r['settled']}, open {r.get('open', 0)}) "
            f"· {r.get('jours', 0)} j · hit {r['hit_pct']:.1f}%\n"
            f"  Profit Kelly {r['kelly_profit_eur']:+.2f} EUR · "
            f"BR finale {r['kelly_br_final']:.2f} EUR\n"
            f"  ROI sur volume {r['kelly_roi_vol_pct']:+.1f}% · "
            f"DD max {r['kelly_max_dd_pct']:.1f}% · "
            f"Sharpe j {r['kelly_sharpe_j']:.2f} · PF {r['kelly_pf']:.2f}"
        )
        if r.get("volume_mise_eur"):
            print(f"  Volume misé {r['volume_mise_eur']:.0f} EUR")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    ap.add_argument(
        "--csv",
        default=os.path.join(ROOT, "data", "backtest_2026_bets.csv"),
        help="Backtest no-leak (0 = skip)",
    )
    ap.add_argument("--live-only", action="store_true")
    ap.add_argument("--backtest-only", action="store_true")
    args = ap.parse_args()

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    if not args.backtest_only and os.path.isfile(args.db):
        min_d = sqlite3.connect(args.db).execute(
            "SELECT MIN(match_date) FROM algo_opportunities WHERE match_date >= ?",
            (args.start,),
        ).fetchone()[0]
        live_rows = []
        for label, pmin in [("Top 5", 0.0), ("Top 5 · proba >= 65%", 0.65)]:
            sel = _select_live(args.db, args.start, p_min_frac=pmin)
            live_rows.append(_kelly_row(sel, ml=ml, label=label))
        _print_block(
            f"LIVE PROD — algo_opportunities depuis {args.start}",
            live_rows,
            note=f"Premiere capture en base : {min_d or 'n/a'}",
        )

    if not args.live_only and args.csv and os.path.isfile(args.csv):
        bt_rows = []
        for label, pmin in [("Top 5", 0.0), ("Top 5 · proba >= 65%", 0.65)]:
            sel = _select_backtest(args.csv, p_min_frac=pmin)
            bt_rows.append(_kelly_row(sel, ml=ml, label=label))
        _print_block(
            "BACKTEST 2026 no-leak — tennis-data janv.-mai (~22/05)",
            bt_rows,
            note=f"CSV : {os.path.relpath(args.csv, ROOT)}",
        )


if __name__ == "__main__":
    main()
