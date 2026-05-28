"""Nombre de paris Kelly pour atteindre un objectif de BR (ex. 1000 EUR depuis 100)."""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import kelly_full_fraction, load_and_filter_bets_csv
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key
from scripts.simulate_top10_proba_2026 import (
    DEFAULT_EXTRA_EXCLUDE,
    DEFAULT_TOP_N,
    KELLY_BASE,
    MAX_STAKE_PCT,
    select_top_proba_per_day,
)


def _bool_won(v) -> bool:
    s = str(v).strip().lower()
    return s in {"1", "true", "yes"}


def prepare_bets_df(
    year: int,
    *,
    ev_min_pct: float = 15.0,
    ev_max_pct: float = 100.0,
    top_n: int = DEFAULT_TOP_N,
) -> pd.DataFrame:
    path = os.path.join(ROOT, "data", f"backtest_{year}_bets.csv")
    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    df_f = load_and_filter_bets_csv(
        path,
        year=year,
        ev_min_pct=ev_min_pct,
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=["G", "M", "A"],
        extra_tournament_tokens=extra,
    )
    df_f = df_f[df_f["ev"].astype(float) <= ev_max_pct / 100.0].reset_index(drop=True)
    df = select_top_proba_per_day(df_f, top_n=int(top_n))
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["_ord"] = np.arange(len(df), dtype=np.int64)
    return (
        df.sort_values(["date", "p_model", "_ord"], ascending=[True, False, True], kind="mergesort")
        .drop(columns=["_ord"])
        .reset_index(drop=True)
    )


def bets_until_bankroll_target(
    df: pd.DataFrame,
    ml: TennisMLModel,
    *,
    bankroll_start: float = 100.0,
    target: float = 1000.0,
) -> dict:
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    adapt_frac = KELLY_BASE
    cap_frac = MAX_STAKE_PCT / 100.0

    br = float(bankroll_start)
    n_bets = 0
    first_bet_date = None
    reach_date = None
    reached = br >= target
    peak_before = br
    max_dd_to_target = 0.0

    for day, day_df in df.groupby("date", sort=True):
        if reached:
            break
        day_df = day_df.reset_index(drop=True)
        b0 = br
        liquid = b0
        day_deploy_cap = b0
        day_deploy_used = 0.0
        day_pnl = 0.0
        reached_midday = False

        for _, row in day_df.iterrows():
            odd = float(row["odd"])
            p = float(row["p_model"])
            won = _bool_won(row["won"])
            sk = resolve_match_brier_segment_key(
                ml,
                tour=row.get("tour"),
                surface=row.get("surface"),
                tournament=row.get("tournament"),
                tourney_level=row.get("tourney_level"),
            )
            brier_seg = float(seg.get(sk, glob_b))
            kf = kelly_full_fraction(p, odd)
            stake_frac = max(0.0, (adapt_frac * kf) * max(0.0, 1.0 - brier_seg / 0.25))
            raw = liquid * stake_frac
            cap_lim = max(0.0, liquid) * cap_frac
            remaining = max(0.0, day_deploy_cap - day_deploy_used)
            stake = max(0.0, min(raw, cap_lim, liquid, remaining))
            if stake <= 0.0:
                continue

            liquid -= stake
            day_deploy_used += stake
            n_bets += 1
            if first_bet_date is None:
                first_bet_date = pd.Timestamp(day)

            pnl = stake * (odd - 1.0) if won else -stake
            day_pnl += pnl
            br_intraday = b0 + day_pnl
            peak_before = max(peak_before, br_intraday)
            if peak_before > 0:
                dd = (peak_before - br_intraday) / peak_before
                max_dd_to_target = max(max_dd_to_target, dd)

            if br_intraday >= target and not reached:
                reached = True
                reached_midday = True
                reach_date = pd.Timestamp(day)
                br = br_intraday
                break

        if reached_midday:
            break
        if not reached:
            br = b0 + day_pnl

    final_br = br
    return {
        "n_bets": n_bets,
        "reached": reached or final_br >= target,
        "bankroll_final": final_br,
        "first_bet_date": first_bet_date,
        "reach_date": reach_date,
        "max_drawdown_pct_to_target": max_dd_to_target * 100.0,
        "total_bets_year": len(df),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2024,2025,2026")
    ap.add_argument("--br-start", type=float, default=100.0)
    ap.add_argument("--target", type=float, default=1000.0)
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    args = ap.parse_args()

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    years = [int(y.strip()) for y in args.years.split(",") if y.strip()]
    print(
        f"Objectif : {args.br_start:.0f} EUR -> {args.target:.0f} EUR "
        f"(Kelly 1/2 x Brier, cap 15%%, top {args.top_n} proba/j, EV 15-100%%)\n"
    )
    for y in years:
        df = prepare_bets_df(y, top_n=int(args.top_n))
        r = bets_until_bankroll_target(
            df, ml, bankroll_start=args.br_start, target=args.target
        )
        if r["reached"]:
            d0 = r["first_bet_date"].strftime("%Y-%m-%d") if r["first_bet_date"] else "?"
            d1 = r["reach_date"].strftime("%Y-%m-%d") if r["reach_date"] else "?"
            days = (
                (r["reach_date"] - r["first_bet_date"]).days + 1
                if r["first_bet_date"] and r["reach_date"]
                else "?"
            )
            print(
                f"  {y}: **{r['n_bets']} paris** pour atteindre {args.target:.0f} EUR "
                f"(BR={r['bankroll_final']:.2f} EUR au passage)"
            )
            print(f"       calendrier ~{d0} -> {d1} ({days} jours)")
            print(f"       max DD avant objectif: {r['max_drawdown_pct_to_target']:.1f} %")
        else:
            print(
                f"  {y}: objectif **non atteint** apres {r['total_bets_year']} paris "
                f"(BR finale Kelly sur l'annee: {r['bankroll_final']:.2f} EUR)"
            )
        print()


if __name__ == "__main__":
    main()
