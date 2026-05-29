#!/usr/bin/env python3
"""Backtest Roland-Garros 2026 — Top 5 proba vs Top 5 EV vs p_model >= 65 %.

Replay depuis ``algo_opportunities`` (cotes / proba / résultats réels capturés).

Usage:
  py -3 scripts/backtest_rg_strategies.py
  py -3 scripts/backtest_rg_strategies.py --end-date 2026-05-28
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import simulate_sequential_intraday
from scripts.compare_top5_proba_vs_ev_2026 import select_top_ev_per_day
from scripts.ml_model import TennisMLModel
from scripts.simulate_top10_proba_2026 import (
    KELLY_BASE,
    MAX_STAKE_PCT,
    _segment_calibration_key,
    flat_stake_metrics,
    select_top_proba_per_day,
)

RG_START = "2026-05-18"
DB_DEFAULT = os.path.join(ROOT, "data", "bettinghud.db")
BR_START = 100.0
EV_MIN_PCT = 15.0
EV_MAX_PCT = 100.0
PMODEL_MIN = 0.65


def _norm_status(x) -> str:
    s = str(x or "").lower()
    if "gagn" in s:
        return "won"
    if "perdu" in s:
        return "lost"
    if "cours" in s:
        return "open"
    return "other"


def load_rg_opportunities(
    *,
    db_path: str,
    start: str = RG_START,
    end: str | None = None,
) -> pd.DataFrame:
    cn = sqlite3.connect(db_path)
    q = """
        SELECT opportunity_key, match_date, detected_date, tournament, tour, status,
               p_model, ev, odd_book, true_odd, bet_on, match_name,
               segment_brier, segment_key
        FROM algo_opportunities
        WHERE (
            lower(tournament) LIKE '%roland%'
            OR lower(tournament) LIKE '%garros%'
            OR lower(tournament) LIKE '%french%'
        )
        AND match_date >= ?
    """
    params: list = [start]
    if end:
        q += " AND match_date <= ?"
        params.append(end)
    df = pd.read_sql_query(q, cn, params=params)
    cn.close()
    if df.empty:
        return df
    df = df.copy()
    df["p_model"] = pd.to_numeric(df["p_model"], errors="coerce")
    df["ev"] = pd.to_numeric(df["ev"], errors="coerce")
    df["ev_pct"] = df["ev"] * 100.0
    df["odd"] = pd.to_numeric(df["odd_book"], errors="coerce")
    df["date"] = pd.to_datetime(df["match_date"], errors="coerce")
    df = df.dropna(subset=["date", "p_model", "odd"])
    df = df[df["odd"] > 1.0].reset_index(drop=True)
    df["st"] = df["status"].map(_norm_status)
    df["won"] = df["st"] == "won"
    df["settled"] = df["st"].isin(["won", "lost"])
    return df


def dedupe_opportunities(df: pd.DataFrame) -> pd.DataFrame:
    """Une ligne par (jour, pari) — garde la meilleure proba si doublons capture."""
    if df.empty:
        return df
    work = df.copy()
    work["_ord"] = np.arange(len(work), dtype=np.int64)
    work = work.sort_values(
        ["date", "bet_on", "match_name", "p_model", "_ord"],
        ascending=[True, True, True, False, True],
        kind="mergesort",
    )
    return (
        work.drop_duplicates(subset=["date", "bet_on", "match_name"], keep="first")
        .drop(columns=["_ord"])
        .reset_index(drop=True)
    )


def apply_ev_band(
    df: pd.DataFrame,
    *,
    ev_min_pct: float = EV_MIN_PCT,
    ev_max_pct: float = EV_MAX_PCT,
) -> pd.DataFrame:
    ev = df["ev_pct"]
    return df[(ev >= float(ev_min_pct)) & (ev <= float(ev_max_pct))].copy()


def select_top5_proba(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work["_ord"] = np.arange(len(work), dtype=np.int64)
    work = work.sort_values(
        ["date", "p_model", "_ord"],
        ascending=[True, False, True],
        kind="mergesort",
    )
    work["_day_rank"] = work.groupby("date", sort=False).cumcount() + 1
    return work.loc[work["_day_rank"] <= 5].drop(columns=["_ord", "_day_rank"]).reset_index(drop=True)


def select_top5_ev(df: pd.DataFrame) -> pd.DataFrame:
    return select_top_ev_per_day(df, top_n=5)


def select_pmodel_min(df: pd.DataFrame, *, p_min: float = PMODEL_MIN) -> pd.DataFrame:
    return df[df["p_model"].astype(float) >= float(p_min)].copy().reset_index(drop=True)


def _prepare_kelly_df(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work = work.sort_values(["date", "p_model"], ascending=[True, False], kind="mergesort")
    work["won"] = work["won"].astype(bool)
    if "segment_calibration_key" not in work.columns:
        work["segment_calibration_key"] = work["segment_key"].fillna("").astype(str)
    return work.reset_index(drop=True)


def metrics_for_selection(
    df: pd.DataFrame,
    *,
    ml: TennisMLModel,
    label: str,
) -> dict:
    settled = df[df["settled"]].copy()
    open_n = int((df["st"] == "open").sum())
    out: dict = {
        "strategie": label,
        "paris_total": len(df),
        "paris_settled": len(settled),
        "paris_open": open_n,
        "jours": int(df["date"].nunique()) if not df.empty else 0,
    }
    if settled.empty:
        out.update(
            {
                "hit_pct": 0.0,
                "profit_1u": 0.0,
                "roi_1u_pct": 0.0,
                "brier": 0.0,
                "kelly_profit_eur": 0.0,
                "kelly_br_final": BR_START,
                "kelly_roi_vol_pct": 0.0,
                "kelly_max_dd_pct": 0.0,
                "kelly_sharpe_j": 0.0,
            }
        )
        return out

    work = settled.copy()
    work["segment_calibration_key"] = work.apply(
        lambda r: _segment_calibration_key(r, ml), axis=1
    )
    flat = flat_stake_metrics(work, br0=BR_START)
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    kdf = _prepare_kelly_df(work)
    kelly = simulate_sequential_intraday(
        kdf,
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
    out.update(
        {
            "hit_pct": float(flat["hit_pct"]),
            "profit_1u": float(flat["net_units"]),
            "roi_1u_pct": float(flat["roi_pct"]),
            "brier": float(flat["brier"]),
            "kelly_profit_eur": float(kelly["net_profit_eur"]),
            "kelly_br_final": float(kelly["bankroll_final"]),
            "kelly_roi_vol_pct": float(kelly["roi_on_staked_pct"]),
            "kelly_max_dd_pct": float(kelly["max_drawdown_pct"]),
            "kelly_sharpe_j": float(kelly["sharpe_daily"]),
            "kelly_pf": float(kelly.get("profit_factor") or 0.0),
        }
    )
    return out


def daily_pnl_1u(df: pd.DataFrame) -> pd.Series:
    settled = df[df["settled"]].copy()
    if settled.empty:
        return pd.Series(dtype=float)
    pnl = np.where(settled["won"], settled["odd"] - 1.0, -1.0)
    settled = settled.assign(_pnl=pnl)
    return settled.groupby(settled["date"].dt.date)["_pnl"].sum()


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest RG 2026 — 3 stratégies")
    ap.add_argument("--start", default=RG_START)
    ap.add_argument("--end", default=None, help="Date fin inclusive (defaut: hier Paris)")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--ev-min-pct", type=float, default=EV_MIN_PCT)
    ap.add_argument("--ev-max-pct", type=float, default=EV_MAX_PCT)
    ap.add_argument("--p-model-min", type=float, default=PMODEL_MIN)
    ap.add_argument("--export", default=None, help="Chemin CSV export")
    args = ap.parse_args()

    end = args.end
    if not end:
        end = date.today().isoformat()

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    raw = load_rg_opportunities(db_path=args.db, start=str(args.start), end=str(end))
    if raw.empty:
        print(f"Aucune opportunite RG dans {args.db} depuis {args.start}")
        return 1

    pool = apply_ev_band(raw, ev_min_pct=args.ev_min_pct, ev_max_pct=args.ev_max_pct)
    pool = dedupe_opportunities(pool)

    strategies = [
        ("Top 5 proba (EV 15-100 %)", select_top5_proba(pool)),
        ("Top 5 EV (EV 15-100 %)", select_top5_ev(pool)),
        (
            f"Tous paris p_model >= {int(args.p_model_min * 100)} % (EV 15-100 %)",
            select_pmodel_min(pool, p_min=args.p_model_min),
        ),
    ]

    print("=" * 92)
    print(f"BACKTEST ROLAND-GARROS 2026 — {args.start} -> {end}")
    print(f"Source : algo_opportunities ({os.path.relpath(args.db, ROOT)})")
    print(
        f"Pool EV favori {args.ev_min_pct:.0f}-{args.ev_max_pct:.0f} % : "
        f"{len(pool)} lignes / {pool['date'].nunique()} jours "
        f"(brut RG : {len(raw)})"
    )
    print(
        f"Mise ref 1 u flat · Kelly 1/2 x Brier cap {MAX_STAKE_PCT:.0f} % · BR {BR_START:.0f} EUR"
    )
    print("=" * 92)
    print(
        f"{'Strategie':<42} {'Tot':>4} {'Set':>4} {'Open':>4} {'Hit%':>6} "
        f"{'ROI1u':>7} {'Pr1u':>7} | {'Kelly€':>8} {'BR':>7} {'DD%':>5}"
    )

    rows_csv: list[dict] = []
    for label, sel in strategies:
        m = metrics_for_selection(sel, ml=ml, label=label)
        sign = "+" if m["profit_1u"] >= 0 else ""
        print(
            f"{label:<42} {m['paris_total']:>4} {m['paris_settled']:>4} {m['paris_open']:>4} "
            f"{m['hit_pct']:>5.1f}% {m['roi_1u_pct']:>+6.1f}% {sign}{m['profit_1u']:>6.1f}u | "
            f"{m['kelly_profit_eur']:>+7.1f} {m['kelly_br_final']:>7.1f} "
            f"{m['kelly_max_dd_pct']:>5.1f}"
        )
        rows_csv.append(
            {
                "periode_debut": args.start,
                "periode_fin": end,
                **m,
            }
        )

    print("\n--- PnL 1u par jour (terminés) ---")
    for label, sel in strategies:
        daily = daily_pnl_1u(sel)
        if daily.empty:
            print(f"{label}: aucun pari terminé")
            continue
        cum = float(daily.sum())
        print(
            f"{label}: cumul {cum:+.2f}u · "
            f"best {daily.max():+.2f}u · worst {daily.min():+.2f}u · "
            f"{len(daily)} jour(s) actif(s)"
        )

    print("\n--- Détail paris terminés Top 5 proba ---")
    top5p = strategies[0][1]
    settled = top5p[top5p["settled"]].sort_values(["date", "p_model"], ascending=[True, False])
    for _, r in settled.iterrows():
        d = r["date"].strftime("%Y-%m-%d") if pd.notna(r["date"]) else "?"
        res = "W" if r["won"] else "L"
        pnl = (float(r["odd"]) - 1.0) if r["won"] else -1.0
        print(
            f"  {d} {res} {str(r['bet_on'])[:22]:<22} "
            f"p={float(r['p_model'])*100:.1f}% ev={float(r['ev_pct']):+.0f}% "
            f"@{float(r['odd']):.2f} -> {pnl:+.2f}u"
        )

    out_dir = os.path.join(ROOT, "data", "reports")
    os.makedirs(out_dir, exist_ok=True)
    out_summary = args.export or os.path.join(out_dir, f"backtest_rg_strategies_{args.start}_{end}.csv")
    pd.DataFrame(rows_csv).to_csv(out_summary, index=False, encoding="utf-8-sig")

    out_bets = os.path.join(out_dir, f"backtest_rg_strategies_bets_{args.start}_{end}.csv")
    all_bets: list[pd.DataFrame] = []
    for label, sel in strategies:
        part = sel.copy()
        part["strategie"] = label
        all_bets.append(part)
    pd.concat(all_bets, ignore_index=True).to_csv(out_bets, index=False, encoding="utf-8-sig")

    print(f"\nExport resume : {os.path.relpath(out_summary, ROOT)}")
    print(f"Export paris : {os.path.relpath(out_bets, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
