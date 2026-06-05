"""Compare strategies: p_model seuils, top5 proba 15-100, top5 EV (1 u + Kelly)."""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import load_and_filter_bets_csv, simulate_sequential_intraday
from scripts.compare_top5_proba_vs_ev_2026 import select_top_ev_per_day
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

STANDARD_EXTRA = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
STANDARD_LEVELS = list(DEFAULT_TOURNEY_LEVELS)
BR_START = 100.0
DEFAULT_PMODEL_MINS = [0.65, 0.70, 0.75, 0.80, 0.85]


def _select_df(year: int, key: str, p_min: float = 0.0) -> pd.DataFrame:
    path = os.path.join(ROOT, "data", f"backtest_{year}_bets.csv")
    if key.startswith("pmodel"):
        df = load_and_filter_bets_csv(path, year=year)
        if p_min > 0:
            df = df[df["p_model"].astype(float) >= p_min]
        return df.reset_index(drop=True)
    if key == "top5_proba":
        df = load_and_filter_bets_csv(
            path,
            year=year,
            ev_min_pct=15.0,
            allowed_tours=["ATP", "WTA"],
            allowed_tourney_levels=STANDARD_LEVELS,
            extra_tournament_tokens=STANDARD_EXTRA,
        )
        df = df[df["ev"].astype(float) <= 1.0].reset_index(drop=True)
        if p_min > 0:
            df = df[df["p_model"].astype(float) >= p_min]
        return select_top_proba_per_day(df, top_n=5)
    if key == "top5_ev":
        df = load_and_filter_bets_csv(
            path,
            year=year,
            ev_min_pct=None,
            allowed_tours=["ATP", "WTA"],
            allowed_tourney_levels=STANDARD_LEVELS,
            extra_tournament_tokens=STANDARD_EXTRA,
        )
        if p_min > 0:
            df = df[df["p_model"].astype(float) >= p_min]
        return select_top_ev_per_day(df, top_n=5)
    raise ValueError(key)


def _metrics(df: pd.DataFrame, ml: TennisMLModel) -> dict:
    if df.empty:
        kelly_empty = {
            "roi_on_staked_pct": 0.0,
            "bankroll_final": BR_START,
            "max_drawdown_pct": 0.0,
            "sharpe_daily": 0.0,
            "profit_factor": 0.0,
        }
        return {
            "paris": 0,
            "jours": 0,
            "hit": 0.0,
            "roi": 0.0,
            "profit": 0.0,
            "brier": 0.0,
            "kelly": kelly_empty,
        }
    work = df.copy()
    work["segment_calibration_key"] = work.apply(
        lambda r: _segment_calibration_key(r, ml), axis=1
    )
    flat = flat_stake_metrics(work, br0=BR_START)
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
        "paris": len(work),
        "jours": int(work["date"].nunique()),
        "hit": flat["hit_pct"],
        "roi": flat["roi_pct"],
        "profit": flat["net_units"],
        "brier": flat["brier"],
        "kelly": kelly,
    }


def _print_block(label: str, rows: list[dict]) -> None:
    print("=" * 92)
    print(label)
    print("=" * 92)
    print(
        f"{'An':>4} {'p%':>4} {'Paris':>6} {'Hit%':>6} {'ROI1u':>7} {'Pr1u':>7} | "
        f"{'ROI_K':>7} {'xBR':>8} {'DD_K':>6} {'Sh':>5} {'PF':>5}"
    )
    for r in rows:
        k = r["kelly"]
        mult = k["bankroll_final"] / BR_START
        p_lab = f"{int(r['p_min']*100)}" if r.get("p_min") else "—"
        print(
            f"{r['year']:>4} {p_lab:>4} {r['paris']:>6} {r['hit']:>5.1f}% {r['roi']:>+6.1f}% "
            f"{r['profit']:>+6.0f}u | {k['roi_on_staked_pct']:>+6.1f}% {mult:>7.1f}x "
            f"{k['max_drawdown_pct']:>5.1f}% {k['sharpe_daily']:>4.2f} "
            f"{k.get('profit_factor') or 0:>5.2f}"
        )
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--p-model-mins",
        type=str,
        default="65,70,75,80,85",
        help="Seuils p_model en %% (ex. 65,70,75,80,85).",
    )
    ap.add_argument("--years", type=str, default="2024,2025,2026")
    args = ap.parse_args()

    p_mins = [float(x.strip()) / 100.0 for x in args.p_model_mins.split(",") if x.strip()]
    years = [int(x.strip()) for x in args.years.split(",") if x.strip()]

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    print(
        f"Kelly: 1/2 x Brier, cap {MAX_STAKE_PCT:.0f}% liquidite, BR {BR_START:.0f} EUR\n"
        f"Annees: {years} | p_model seuils: {[int(p*100) for p in p_mins]} %\n"
    )

    all_csv: list[dict] = []

    # A) Tous paris, seulement p_model >= seuil
    rows_a: list[dict] = []
    for y in years:
        for p in p_mins:
            df = _select_df(y, "pmodel", p)
            m = _metrics(df, ml)
            row = {
                "strategie": "tous_paris_pmodel",
                "year": y,
                "p_min": p,
                "paris": m["paris"],
                "hit": m["hit"],
                "roi": m["roi"],
                "profit": m["profit"],
                "brier": m["brier"],
                "kelly": m["kelly"],
            }
            rows_a.append(row)
            all_csv.append(_flat_row(row))
    _print_block("A) Tous paris · filtre p_model uniquement (pas EV, pas top 5)", rows_a)

    # B) Top 5 proba 15-100 (reference, sans p_model)
    rows_b: list[dict] = []
    for y in years:
        df = _select_df(y, "top5_proba", 0.0)
        m = _metrics(df, ml)
        row = {
            "strategie": "top5_proba_15_100",
            "year": y,
            "p_min": 0.0,
            "paris": m["paris"],
            "hit": m["hit"],
            "roi": m["roi"],
            "profit": m["profit"],
            "brier": m["brier"],
            "kelly": m["kelly"],
        }
        rows_b.append(row)
        all_csv.append(_flat_row(row))
    _print_block("B) Reference: Top 5 proba · EV 15-100% · G/M/A (sans p_model min)", rows_b)

    # C) Top 5 proba 15-100 + chaque seuil p_model sur le pool
    rows_c: list[dict] = []
    for y in years:
        for p in p_mins:
            df = _select_df(y, "top5_proba", p)
            m = _metrics(df, ml)
            row = {
                "strategie": "top5_proba_15_100_pmodel",
                "year": y,
                "p_min": p,
                "paris": m["paris"],
                "hit": m["hit"],
                "roi": m["roi"],
                "profit": m["profit"],
                "brier": m["brier"],
                "kelly": m["kelly"],
            }
            rows_c.append(row)
            all_csv.append(_flat_row(row))
    _print_block("C) Top 5 proba · EV 15-100% · + p_model >= seuil", rows_c)

    # D) Top 5 EV sans filtre EV (reference)
    rows_d: list[dict] = []
    for y in years:
        df = _select_df(y, "top5_ev", 0.0)
        m = _metrics(df, ml)
        row = {
            "strategie": "top5_ev_no_ev",
            "year": y,
            "p_min": 0.0,
            "paris": m["paris"],
            "hit": m["hit"],
            "roi": m["roi"],
            "profit": m["profit"],
            "brier": m["brier"],
            "kelly": m["kelly"],
        }
        rows_d.append(row)
        all_csv.append(_flat_row(row))
    _print_block("D) Reference: Top 5 EV · sans filtre EV · G/M/A", rows_d)

    out = os.path.join(ROOT, "data", "reports", "compare_three_strategies_pmodel_grid.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    pd.DataFrame(all_csv).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"Export: {os.path.relpath(out, ROOT)}")


def _flat_row(r: dict) -> dict:
    k = r["kelly"]
    return {
        "strategie": r["strategie"],
        "annee": r["year"],
        "p_model_min_pct": int(r["p_min"] * 100) if r["p_min"] else 0,
        "paris": r["paris"],
        "hit_pct": round(r["hit"], 2),
        "roi_1u_pct": round(r["roi"], 2),
        "profit_1u": round(r["profit"], 2),
        "brier": round(r["brier"], 4),
        "roi_kelly_vol_pct": round(k["roi_on_staked_pct"], 2),
        "br_kelly": round(k["bankroll_final"], 0),
        "mult_br": round(k["bankroll_final"] / BR_START, 2),
        "dd_kelly_pct": round(k["max_drawdown_pct"], 2),
        "sharpe_j": round(k["sharpe_daily"], 2),
        "pf": round(k.get("profit_factor") or 0, 2),
    }


if __name__ == "__main__":
    main()
