"""Grille top N probas/jour · EV 15–100 % · Kelly ½×Brier · par année et circuit."""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import load_and_filter_bets_csv, simulate_sequential_intraday
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

DEFAULT_TOPNS = [5, 10, 15, 20]
DEFAULT_YEARS = [2024, 2025, 2026]
CIRCUITS = {"ALL": ["ATP", "WTA"], "ATP": ["ATP"], "WTA": ["WTA"]}


def run_grid(
    years: list[int],
    *,
    top_ns: list[int],
    ev_min_pct: float = 15.0,
    ev_max_pct: float = 100.0,
    br_start: float = 100.0,
) -> pd.DataFrame:
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]

    rows: list[dict] = []
    for year in years:
        path = os.path.join(ROOT, "data", f"backtest_{year}_bets.csv")
        if not os.path.isfile(path):
            print(f"[WARN] CSV absent : {path}", file=sys.stderr)
            continue
        for circuit, tours in CIRCUITS.items():
            df_f = load_and_filter_bets_csv(
                path,
                year=year,
                ev_min_pct=float(ev_min_pct),
                allowed_tours=tours,
                allowed_tourney_levels=list(DEFAULT_TOURNEY_LEVELS),
                extra_tournament_tokens=extra,
            )
            df_f = df_f[df_f["ev"].astype(float) <= float(ev_max_pct) / 100.0].reset_index(drop=True)
            for top_n in top_ns:
                df_sel = select_top_proba_per_day(df_f, top_n=int(top_n))
                if df_sel.empty:
                    rows.append(
                        {
                            "Annee": year,
                            "Circuit": circuit,
                            "Top": top_n,
                            "Paris": 0,
                            "Hit_pct": 0.0,
                            "ROI_1u_pct": 0.0,
                            "Profit_1u": 0.0,
                            "ROI_Kelly_vol_pct": 0.0,
                            "Sharpe_j": 0.0,
                            "MaxDD_Kelly_pct": 0.0,
                        }
                    )
                    continue
                df_sel = df_sel.copy()
                df_sel["segment_calibration_key"] = df_sel.apply(
                    lambda r: _segment_calibration_key(r, ml), axis=1
                )
                flat = flat_stake_metrics(df_sel, br0=float(br_start))
                kelly = simulate_sequential_intraday(
                    df_sel,
                    bankroll_start=float(br_start),
                    kelly_multiplier=1.0,
                    max_stake_pct=MAX_STAKE_PCT,
                    daily_stake_budget_pct=100.0,
                    use_adaptive_kelly_quarter=True,
                    adaptive_kelly_base_fraction=KELLY_BASE,
                    segment_brier_scores=seg,
                    global_brier_score=glob_b,
                    stake_cap_basis="liquid",
                )
                rows.append(
                    {
                        "Annee": year,
                        "Circuit": circuit,
                        "Top": top_n,
                        "Paris": int(flat["n_bets"]),
                        "Hit_pct": round(float(flat["hit_pct"]), 1),
                        "ROI_1u_pct": round(float(flat["roi_pct"]), 2),
                        "Profit_1u": round(float(flat["net_units"]), 1),
                        "ROI_Kelly_vol_pct": round(float(kelly["roi_on_staked_pct"]), 2),
                        "Sharpe_j": round(float(kelly["sharpe_daily"]), 2),
                        "MaxDD_Kelly_pct": round(float(kelly["max_drawdown_pct"]), 1),
                    }
                )
    return pd.DataFrame(rows)


def _print_year_tables(df: pd.DataFrame) -> None:
    cols = ["Circuit", "Top", "Paris", "Hit_pct", "ROI_1u_pct", "Profit_1u", "ROI_Kelly_vol_pct", "Sharpe_j", "MaxDD_Kelly_pct"]
    for year in sorted(df["Annee"].unique()):
        sub = df[df["Annee"] == year][cols].sort_values(["Circuit", "Top"])
        print(f"\n=== {int(year)} — EV 15–100 % · top N/jour par p_model · Kelly ½×Brier · cap 15 %/pari ===")
        print(sub.to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default=",".join(str(y) for y in DEFAULT_YEARS))
    ap.add_argument("--top-ns", default=",".join(str(n) for n in DEFAULT_TOPNS))
    ap.add_argument("--ev-min-pct", type=float, default=15.0)
    ap.add_argument("--ev-max-pct", type=float, default=100.0)
    ap.add_argument("--br-start", type=float, default=100.0)
    ap.add_argument(
        "--out",
        default=os.path.join("data", "reports", "compare_topn_grid_by_year.csv"),
    )
    args = ap.parse_args()

    years = [int(y.strip()) for y in str(args.years).split(",") if y.strip()]
    top_ns = [int(n.strip()) for n in str(args.top_ns).split(",") if n.strip()]

    df = run_grid(
        years,
        top_ns=top_ns,
        ev_min_pct=float(args.ev_min_pct),
        ev_max_pct=float(args.ev_max_pct),
        br_start=float(args.br_start),
    )
    if df.empty:
        raise SystemExit("Aucun résultat.")

    out_path = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")

    _print_year_tables(df)
    print(f"\nExport : {os.path.relpath(out_path, ROOT)}")


if __name__ == "__main__":
    main()
