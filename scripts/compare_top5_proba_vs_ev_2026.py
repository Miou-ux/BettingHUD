"""Compare top N proba/jour vs top N EV/jour sur CSV backtest no-leak.

Usage:
    py -3 scripts/compare_top5_proba_vs_ev_2026.py --year 2026 --ev-mins 5,10,15,20
    py -3 scripts/compare_top5_proba_vs_ev_2026.py --years 2024,2025,2026 --ev-mins 5,10,15,20
"""
from __future__ import annotations

import os
import sys

import numpy as np
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


def select_top_ev_per_day(df: pd.DataFrame, *, top_n: int = 5) -> pd.DataFrame:
    if df.empty or top_n <= 0:
        return df.iloc[0:0].copy()
    work = df.copy()
    work["_ord"] = np.arange(len(work), dtype=np.int64)
    work = work.sort_values(
        ["date", "ev", "_ord"],
        ascending=[True, False, True],
        kind="mergesort",
    )
    work["_day_rank"] = work.groupby("date", sort=False).cumcount() + 1
    return work.loc[work["_day_rank"] <= int(top_n)].drop(
        columns=["_ord", "_day_rank"]
    ).reset_index(drop=True)


def run_scenario(
    csv_path: str,
    *,
    year: int,
    mode: str,
    ev_min_pct: float,
    ev_max_pct: float = 100.0,
    top_n: int = 5,
    br_start: float = 100.0,
    ml: TennisMLModel,
) -> dict:
    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    df_f = load_and_filter_bets_csv(
        csv_path,
        year=year,
        ev_min_pct=float(ev_min_pct),
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=list(DEFAULT_TOURNEY_LEVELS),
        extra_tournament_tokens=extra,
    )
    df_f = df_f[df_f["ev"].astype(float) <= float(ev_max_pct) / 100.0].reset_index(drop=True)
    if mode == "proba":
        df_sel = select_top_proba_per_day(df_f, top_n=int(top_n))
        label = f"Top {top_n} proba"
    elif mode == "ev":
        df_sel = select_top_ev_per_day(df_f, top_n=int(top_n))
        label = f"Top {top_n} EV"
    else:
        raise ValueError(mode)

    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    if not df_sel.empty:
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
    return {
        "label": label,
        "mode": mode,
        "ev_min_pct": ev_min_pct,
        "ev_max_pct": ev_max_pct,
        "pool": len(df_f),
        "bets": len(df_sel),
        "days": int(df_sel["date"].nunique()) if not df_sel.empty else 0,
        "flat": flat,
        "kelly": kelly,
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument(
        "--years",
        type=str,
        default=None,
        help="Plusieurs annees (ex. 2024,2025,2026). Remplace --year.",
    )
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument(
        "--ev-mins",
        type=str,
        default="5,10,15,20",
        help="Seuils EV min en %% (ex. 5,10,15,20).",
    )
    ap.add_argument("--ev-max-pct", type=float, default=None, help="Un seul EV max (%%).")
    ap.add_argument(
        "--ev-max-pcts",
        type=str,
        default=None,
        help="Plusieurs EV max (ex. 100,150,200). Remplace --ev-max-pct.",
    )
    ap.add_argument("--csv", type=str, default=None)
    args = ap.parse_args()

    years = (
        [int(y.strip()) for y in str(args.years).split(",") if y.strip()]
        if args.years
        else [int(args.year)]
    )
    top_n = int(args.top_n)
    ev_mins = [float(x.strip()) for x in str(args.ev_mins).split(",") if x.strip()]
    if args.ev_max_pcts:
        ev_maxs = [float(x.strip()) for x in str(args.ev_max_pcts).split(",") if x.strip()]
    elif args.ev_max_pct is not None:
        ev_maxs = [float(args.ev_max_pct)]
    else:
        ev_maxs = [100.0]
    ev_min_band = ", ".join(f"{e:.0f}" for e in ev_mins)
    ev_max_band = ", ".join(f"{e:.0f}" for e in ev_maxs)

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    all_rows_csv: list[dict] = []

    for year in years:
        csv_path = args.csv or os.path.join(ROOT, "data", f"backtest_{year}_bets.csv")
        if not os.path.isfile(csv_path):
            print(f"[WARN] CSV introuvable: {csv_path}", file=sys.stderr)
            continue

        for ev_max_pct in ev_maxs:
            scenarios = [
                (mode, ev_min, ev_max_pct)
                for ev_min in ev_mins
                for mode in ("proba", "ev")
            ]
            results = [
                run_scenario(
                    csv_path,
                    year=year,
                    mode=mode,
                    ev_min_pct=ev_min,
                    ev_max_pct=float(ev_max),
                    top_n=top_n,
                    ml=ml,
                )
                for mode, ev_min, ev_max in scenarios
            ]

            print("=" * 88)
            print(
                f"BACKTEST {year} — Top {top_n} proba vs Top {top_n} EV · "
                f"EV min [{ev_min_band}] % · max {ev_max_pct:.0f} % · G/M/A · no-leak"
            )
            print(f"CSV: {os.path.relpath(csv_path, ROOT)}")
            print("=" * 88)
            hdr = (
                f"{'Strategie':<16} {'EV min':>7} {'Pool':>6} {'Paris':>6} {'Jours':>6} "
                f"{'Hit%':>7} {'ROI 1u':>9} {'Profit 1u':>10} {'Brier':>7}"
            )
            print(hdr)
            for r in results:
                f = r["flat"]
                print(
                    f"{r['label']:<16} {r['ev_min_pct']:>6.0f}% {r['pool']:>6} {r['bets']:>6} {r['days']:>6} "
                    f"{f['hit_pct']:>6.1f}% {f['roi_pct']:>+8.2f}% {f['net_units']:>+9.1f}u "
                    f"{f['brier']:>6.4f}"
                )
            print("\n--- Proba vs EV par EV min (ROI 1 u) ---")
            for ev_min in ev_mins:
                proba = next(
                    x for x in results if x["mode"] == "proba" and x["ev_min_pct"] == ev_min
                )
                ev_row = next(x for x in results if x["mode"] == "ev" and x["ev_min_pct"] == ev_min)
                fp, fe = proba["flat"], ev_row["flat"]
                winner = "proba" if fp["roi_pct"] >= fe["roi_pct"] else "EV"
                print(
                    f"  min {ev_min:.0f}% : proba ROI={fp['roi_pct']:+.2f}% hit={fp['hit_pct']:.1f}% | "
                    f"EV ROI={fe['roi_pct']:+.2f}% hit={fe['hit_pct']:.1f}% -> {winner}"
                )
            print()

            for r in results:
                f, k = r["flat"], r["kelly"]
                all_rows_csv.append(
                    {
                        "annee": year,
                        "strategie": r["label"],
                        "mode": r["mode"],
                        "ev_min_pct": r["ev_min_pct"],
                        "ev_max_pct": r["ev_max_pct"],
                        "pool": r["pool"],
                        "paris": r["bets"],
                        "jours": r["days"],
                        "hit_pct": round(f["hit_pct"], 2),
                        "roi_1u_pct": round(f["roi_pct"], 2),
                        "profit_1u": round(f["net_units"], 2),
                        "dd_1u_pct": round(f.get("max_drawdown_pct", 0), 2),
                        "brier": round(f["brier"], 4),
                        "roi_kelly_vol_pct": round(k["roi_on_staked_pct"], 2),
                        "br_kelly": round(k["bankroll_final"], 0),
                        "mult_br": round(k["bankroll_final"] / 100, 2),
                        "dd_kelly_pct": round(k["max_drawdown_pct"], 2),
                        "sharpe_j": round(k["sharpe_daily"], 2),
                        "pf": round(k.get("profit_factor") or 0, 2),
                    }
                )

    if all_rows_csv:
        os.makedirs(os.path.join(ROOT, "data", "reports"), exist_ok=True)
        suffix = (
            f"_{years[0]}_{years[-1]}_evmax{int(ev_maxs[0])}"
            if len(ev_maxs) == 1
            else f"_{years[0]}_{years[-1]}_evmax_grid"
        )
        out_all = os.path.join(
            ROOT, "data", "reports", f"compare_top{top_n}_proba_vs_ev{suffix}.csv"
        )
        pd.DataFrame(all_rows_csv).to_csv(out_all, index=False, encoding="utf-8-sig")
        print(f"Export combine : {os.path.relpath(out_all, ROOT)} ({len(all_rows_csv)} lignes)")


if __name__ == "__main__":
    main()
