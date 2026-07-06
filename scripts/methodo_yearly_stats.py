#!/usr/bin/env python3
"""Yearly stats for CourtAlpha /methodo backtest table (hybrid prod Top 5 + 1D1P).

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

from scripts.backtest_pack12_global_2026 import (  # noqa: E402
    BR_START,
    LIVE_CUTOFF,
    MAX_STAKE_PCT,
    _live_rows,
)
from scripts.backtest_prod_1d1p_2026 import picks_for_rows as d1p_picks_for_rows  # noqa: E402
from scripts.backtest_prod_top5_2026 import (  # noqa: E402
    _csv_rows_for_year,
    _kelly_sim,
    picks_for_rows as top5_picks_for_rows,
)
from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.simulate_top10_proba_2026 import (  # noqa: E402
    DEFAULT_TOURNEY_LEVELS,
    KELLY_BASE,
    flat_stake_metrics,
)

TOP_N = 5


def _pool_rows(year: int) -> list[dict]:
    rows = _csv_rows_for_year(year)
    if year >= 2026:
        rows = rows + _live_rows()
    return rows


def _picks_to_flat_df(picks: list[dict]) -> pd.DataFrame:
    rows: list[dict] = []
    for p in picks:
        if p.get("settled") is False:
            continue
        st = str(p.get("status") or "").strip().lower()
        if "annul" in st:
            continue
        if "gagn" in st:
            won = True
        elif "perdu" in st:
            won = False
        elif "won" in p:
            won = bool(p.get("won"))
        else:
            continue
        rows.append(
            {
                "date": str(p.get("calendar_date") or "")[:10],
                "p_model": float(p.get("p_model_fav") or p.get("p_model") or 0),
                "odd": float(p.get("odd_fav") or p.get("odd") or 0),
                "won": bool(won),
            }
        )
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["date"] = pd.to_datetime(out["date"])
    return out


def _kelly_volume_roi(picks: list[dict], ml: TennisMLModel) -> float:
    if not picks:
        return 0.0
    k = _kelly_sim(picks, ml, br_start=BR_START, kelly_frac=KELLY_BASE)
    staked = float(k.get("total_staked_eur") or 0.0)
    net = float(k.get("net_profit_eur") or 0.0)
    if staked <= 0:
        return 0.0
    return net / staked * 100.0


def compute_year_row(year: int, *, ml: TennisMLModel) -> dict | None:
    try:
        pool = _pool_rows(year)
    except Exception:
        return None
    if not pool:
        return None

    top5 = top5_picks_for_rows(pool, limit=TOP_N)
    pick1d1p = d1p_picks_for_rows(pool)
    if not top5 and not pick1d1p:
        return None

    flat_top5 = flat_stake_metrics(_picks_to_flat_df(top5), br0=BR_START)
    flat_1d1p = flat_stake_metrics(_picks_to_flat_df(pick1d1p), br0=BR_START)
    roi_year = _kelly_volume_roi(top5, ml)

    note = None
    if year == 2026:
        note = "partial"

    days_top5 = len({str(p.get("calendar_date") or "")[:10] for p in top5})
    days_1d1p = len({str(p.get("calendar_date") or "")[:10] for p in pick1d1p})

    return {
        "year": str(year),
        "bets": int(flat_top5["n_bets"]),
        "days": days_top5,
        "hit_pct": round(float(flat_top5["hit_pct"]), 1),
        "roi_year_pct": round(roi_year, 1),
        "roi_1d1p_pct": round(float(flat_1d1p["roi_pct"]), 1),
        "brier": round(float(flat_top5["brier"]), 3) if flat_top5["n_bets"] else 0.0,
        "days_1d1p": days_1d1p,
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
            "selection": (
                f"Hybrid Top {TOP_N}: proba ≥ 80%, reliability ≥ 80, "
                "EV tier 1: 15–30%, tier 2: 30–50%, max 5/day"
            ),
            "tours": ["ATP", "WTA"],
            "levels": list(DEFAULT_TOURNEY_LEVELS),
            "roi_year": f"Kelly {KELLY_BASE} × Brier segment, cap 15% liquidity → ROI on staked",
            "roi_1d1p": "Hybrid rank 1/day (1 Day 1 Pick), flat 1 unit",
            "live_replay_from": LIVE_CUTOFF,
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

    print("Methodo yearly stats (hybrid Top 5 + 1D1P)")
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
