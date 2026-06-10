#!/usr/bin/env python3
"""Yearly stats for CourtAlpha /methodo backtest table.

Top 5 proba/jour (EV 15–100 %, G/M/A) — same protocol as methodoContent:
  - bets, hit %, Brier from Top 5 selection
  - roi_year_pct: Kelly ½ × Brier, cap 15 % liq. → roi_on_staked_pct (volume-weighted)
  - roi_1d1p_pct: 1 Day 1 Pick — best rank=1 ATP vs WTA per day, flat 1 u

Usage:
  py -3 scripts/methodo_yearly_stats.py
  py -3 scripts/methodo_yearly_stats.py --years 2024,2025,2026 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import (  # noqa: E402
    load_and_filter_bets_csv,
    resolve_backtest_csv,
    simulate_sequential_intraday,
)
from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.simulate_top10_proba_2026 import (  # noqa: E402
    DEFAULT_EXTRA_EXCLUDE,
    DEFAULT_TOURNEY_LEVELS,
    KELLY_BASE,
    MAX_STAKE_PCT,
    _segment_calibration_key,
    flat_stake_metrics,
    select_top_proba_per_day,
)

BR_START = 100.0
EV_MIN_PCT = 15.0
EV_MAX_PCT = 100.0
TOP_N = 5


def _filter_pool(csv_path: str, *, year: int) -> pd.DataFrame:
    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    df = load_and_filter_bets_csv(
        csv_path,
        year=year,
        ev_min_pct=EV_MIN_PCT,
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=list(DEFAULT_TOURNEY_LEVELS),
        extra_tournament_tokens=extra,
    )
    return df[df["ev"].astype(float) <= EV_MAX_PCT / 100.0].reset_index(drop=True)


def select_one_day_one_pick_per_day(df: pd.DataFrame) -> pd.DataFrame:
    """Best p_model rank=1 ATP vs WTA per calendar day (1 Day 1 Pick backtest proxy)."""
    if df.empty:
        return df.iloc[0:0].copy()
    work = df.copy()
    work["_ord"] = np.arange(len(work), dtype=np.int64)
    work = work.sort_values(
        ["date", "tour", "p_model", "_ord"],
        ascending=[True, True, False, True],
        kind="mergesort",
    )
    # rank=1 per circuit per day
    rank1 = work.groupby(["date", "tour"], sort=False).head(1)
    # best between ATP and WTA (ATP tie-break, aligned one_day_one_pick.py)
    rank1 = rank1.copy()
    rank1["_atp_pref"] = (rank1["tour"].astype(str).str.upper() == "ATP").astype(int)
    rank1 = rank1.sort_values(
        ["date", "p_model", "_atp_pref", "_ord"],
        ascending=[True, False, False, True],
        kind="mergesort",
    )
    return rank1.groupby("date", sort=False).head(1).drop(columns=["_ord", "_atp_pref"]).reset_index(drop=True)


def _attach_segments(df: pd.DataFrame, ml: TennisMLModel) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["segment_calibration_key"] = out.apply(
        lambda r: _segment_calibration_key(r, ml), axis=1
    )
    return out


def _kelly_volume_roi(df: pd.DataFrame, ml: TennisMLModel) -> float:
    if df.empty:
        return 0.0
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    ordered = df.sort_values(["date", "p_model"], ascending=[True, False], kind="mergesort")
    kelly = simulate_sequential_intraday(
        ordered,
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
    return float(kelly["roi_on_staked_pct"])


def compute_year_row(year: int, *, ml: TennisMLModel) -> dict | None:
    csv_path = resolve_backtest_csv(ROOT, year)
    if not csv_path or not os.path.isfile(csv_path):
        return None

    pool = _filter_pool(csv_path, year=year)
    top5 = select_top_proba_per_day(pool, top_n=TOP_N)
    top5 = _attach_segments(top5, ml)
    flat_top5 = flat_stake_metrics(top5, br0=BR_START)

    pick1d1p = select_one_day_one_pick_per_day(pool)
    flat_1d1p = flat_stake_metrics(pick1d1p, br0=BR_START)
    roi_year = _kelly_volume_roi(top5, ml)

    note = None
    if year == 2026:
        note = "partial"

    return {
        "year": str(year),
        "bets": int(flat_top5["n_bets"]),
        "days": int(top5["date"].nunique()) if not top5.empty else 0,
        "hit_pct": round(float(flat_top5["hit_pct"]), 1),
        "roi_year_pct": round(roi_year, 1),
        "roi_1d1p_pct": round(float(flat_1d1p["roi_pct"]), 1),
        "brier": round(float(flat_top5["brier"]), 3),
        "days_1d1p": int(pick1d1p["date"].nunique()) if not pick1d1p.empty else 0,
        "note": note,
    }


def build_methodo_yearly_stats(*, years: list[int] | None = None) -> dict:
    if years is None:
        years = [2024, 2025, 2026]
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    rows: list[dict] = []
    for year in years:
        row = compute_year_row(year, ml=ml)
        if row is not None:
            rows.append(row)

    return {
        "protocol": {
            "selection": f"Top {TOP_N} proba/jour",
            "ev_band_pct": [EV_MIN_PCT, EV_MAX_PCT],
            "tours": ["ATP", "WTA"],
            "levels": list(DEFAULT_TOURNEY_LEVELS),
            "roi_year": "Kelly 1/2 x Brier segment, cap 15% liquidity -> net_profit / total_staked",
            "roi_1d1p": "1 pick/jour (meilleur rank=1 ATP vs WTA), mise fixe 1 u",
        },
        "rows": rows,
        "generated_from": "scripts/methodo_yearly_stats.py",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2024,2025,2026")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    years = [int(y.strip()) for y in args.years.split(",") if y.strip()]
    payload = build_methodo_yearly_stats(years=years)

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print("Methodo yearly stats (Top 5 proba + 1D1P flat)")
    print("-" * 72)
    for r in payload["rows"]:
        note = f" ({r['note']})" if r.get("note") else ""
        print(
            f"{r['year']}{note}: {r['bets']} bets · hit {r['hit_pct']}% · "
            f"ROI year {r['roi_year_pct']:+.1f}% · ROI 1D1P {r['roi_1d1p_pct']:+.1f}% · "
            f"Brier {r['brier']}"
        )


if __name__ == "__main__":
    main()
