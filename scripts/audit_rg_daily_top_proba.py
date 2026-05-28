#!/usr/bin/env python3
"""PnL RG depuis daily_top_proba_picks (replay reel), hors jour courant."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import simulate_sequential_intraday
from scripts.ml_model import TennisMLModel
from scripts.priority_scoring import priority_score_composite
from scripts.value_detector import ValueDetector

RG_START = "2026-05-18"
RG_END_EXCL_TODAY = "2026-05-26"
DB = os.path.join(ROOT, "data", "bettinghud.db")
BR0 = 100.0
EV_MIN_PCT = 15.0
EV_MAX_PCT = 100.0


def _load_picks(*, end_date: str = RG_END_EXCL_TODAY) -> pd.DataFrame:
    cn = sqlite3.connect(DB)
    df = pd.read_sql_query(
        """
        SELECT calendar_date, match_date, tour, rank, top_limit, match_name, fav_player,
               p_model_fav, odd_fav, ev_fav_pct, segment_key, segment_brier, theoretical_stake_frac,
               theoretical_profit, status, capture_source, pick_key
        FROM daily_top_proba_picks
        WHERE calendar_date >= ? AND calendar_date <= ?
        ORDER BY calendar_date, tour, rank
        """,
        cn,
        params=(RG_START, end_date),
    )
    cn.close()
    df["p_model_fav"] = pd.to_numeric(df["p_model_fav"], errors="coerce")
    df["odd_fav"] = pd.to_numeric(df["odd_fav"], errors="coerce")
    df["ev_fav_pct"] = pd.to_numeric(df["ev_fav_pct"], errors="coerce")
    df["theoretical_stake_frac"] = pd.to_numeric(df["theoretical_stake_frac"], errors="coerce")
    df["theoretical_profit"] = pd.to_numeric(df["theoretical_profit"], errors="coerce")
    st = df["status"].astype(str).str.lower()
    df["won"] = st.str.contains("gagn")
    df["lost"] = st.str.contains("perdu")
    df["settled"] = df["won"] | df["lost"]
    df["open"] = st.str.contains("cours")
    return df


def apply_ev_fav_band(
    df: pd.DataFrame,
    *,
    ev_min_pct: float = EV_MIN_PCT,
    ev_max_pct: float = EV_MAX_PCT,
) -> pd.DataFrame:
    """Meme bande que le toggle live : EV favori modele entre min et max inclus."""
    ev = df["ev_fav_pct"]
    return df[(ev >= float(ev_min_pct)) & (ev <= float(ev_max_pct))].copy()


def enrich_composite_scores(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    sharpe_vals: list[float] = []
    priority_vals: list[float] = []
    for _, row in out.iterrows():
        sr = ValueDetector.bet_sharpe_ratio(row.get("p_model_fav"), row.get("odd_fav"))
        brier = float(row.get("segment_brier") or 0.1741)
        sharpe_vals.append(float(sr))
        priority_vals.append(float(priority_score_composite(sr, brier)))
    out["sharpe_ratio"] = sharpe_vals
    out["priority_score"] = priority_vals
    return out


def _select_topn(
    df: pd.DataFrame,
    top_n: int,
    *,
    mode: str = "per_tour",
    rank_by: str = "proba",
) -> pd.DataFrame:
    if top_n <= 0 or df.empty:
        return df.iloc[0:0].copy()

    if rank_by == "composite":
        sort_cols = ["priority_score", "p_model_fav", "rank"]
        sort_asc = [False, False, True]
    else:
        sort_cols = ["p_model_fav", "rank"]
        sort_asc = [False, True]

    if mode == "per_tour":
        parts: list[pd.DataFrame] = []
        for (_cal, _tour), grp in df.groupby(["calendar_date", "tour"], sort=True):
            work = grp.sort_values(sort_cols, ascending=sort_asc, kind="mergesort")
            parts.append(work.head(int(top_n)))
        return pd.concat(parts, ignore_index=True) if parts else df.iloc[0:0].copy()

    work = df.sort_values(
        ["calendar_date", *sort_cols],
        ascending=[True, *sort_asc],
        kind="mergesort",
    )
    work["_day_rank"] = work.groupby("calendar_date", sort=False).cumcount() + 1
    return work.loc[work["_day_rank"] <= top_n].drop(columns=["_day_rank"]).copy()


def _flat_metrics(sub: pd.DataFrame) -> dict:
    settled = sub[sub["settled"]].copy()
    if settled.empty:
        return {"n_settled": 0, "n_open": int(sub["open"].sum()), "hit_pct": 0.0, "profit_1u": 0.0, "roi_1u_pct": 0.0}
    pnl = np.where(settled["won"], settled["odd_fav"] - 1.0, -1.0)
    n = len(settled)
    return {
        "n_settled": n,
        "n_open": int(sub["open"].sum()),
        "hit_pct": float(settled["won"].mean() * 100.0),
        "profit_1u": float(pnl.sum()),
        "roi_1u_pct": float(pnl.sum() / n * 100.0),
    }


def _kelly_metrics(sub: pd.DataFrame, *, br0: float = BR0, rank_by: str = "proba") -> dict:
    settled = sub[sub["settled"]].copy()
    if settled.empty:
        return {"n_settled": 0, "kelly_profit_eur": 0.0, "kelly_roi_vol_pct": 0.0, "br_final": br0}
    if rank_by == "composite":
        settled = settled.sort_values(
            ["calendar_date", "tour", "priority_score", "p_model_fav"],
            ascending=[True, True, False, False],
            kind="mergesort",
        )
    else:
        settled = settled.sort_values(["calendar_date", "tour", "rank"], kind="mergesort")
    work = settled.reset_index(drop=True)
    work = work.rename(columns={"calendar_date": "date", "p_model_fav": "p_model", "odd_fav": "odd"})
    work["won"] = work["won"].astype(bool)
    work["segment_calibration_key"] = work["segment_key"].fillna("").astype(str)
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    sim = simulate_sequential_intraday(
        work,
        bankroll_start=float(br0),
        kelly_multiplier=1.0,
        max_stake_pct=15.0,
        daily_stake_budget_pct=100.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=0.5,
        segment_brier_scores=seg,
        global_brier_score=glob_b,
        stake_cap_basis="liquid",
    )
    return {
        "n_settled": len(settled),
        "kelly_profit_eur": float(sim["net_profit_eur"]),
        "kelly_stake_eur": float(sim["total_staked_eur"]),
        "kelly_roi_vol_pct": float(sim["roi_on_staked_pct"]),
        "br_final": float(sim["bankroll_final"]),
        "max_dd_pct": float(sim["max_drawdown_pct"]),
    }


def _print_block(
    title: str,
    df: pd.DataFrame,
    top_ns: tuple[int, ...],
    *,
    rank_by: str = "proba",
) -> None:
    rank_label = "composite (priority_score)" if rank_by == "composite" else "proba favori"
    print(f"\n=== {title} · tri {rank_label} ===")
    print(f"Picks apres EV {EV_MIN_PCT:.0f}-{EV_MAX_PCT:.0f}% favori : {len(df)}")
    if df.empty:
        return
    for n in top_ns:
        sub = _select_topn(df, n, mode="per_tour", rank_by=rank_by)
        flat = _flat_metrics(sub)
        kelly = _kelly_metrics(sub, rank_by=rank_by)
        sign = "+" if flat["profit_1u"] >= 0 else ""
        print(
            f"Top {n:2d}/tour/jour | settled {flat['n_settled']:3d} open {flat['n_open']:2d} | "
            f"hit {flat['hit_pct']:5.1f}% | 1u {sign}{flat['profit_1u']:.2f}u ({sign}{flat['roi_1u_pct']:.1f}% ROI) | "
            f"Kelly {kelly['kelly_profit_eur']:+.1f} EUR -> BR {kelly['br_final']:.1f} EUR"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit RG daily top probas replay")
    parser.add_argument("--end-date", default=RG_END_EXCL_TODAY)
    parser.add_argument("--ev-min-pct", type=float, default=EV_MIN_PCT)
    parser.add_argument("--ev-max-pct", type=float, default=EV_MAX_PCT)
    args = parser.parse_args()

    raw = _load_picks(end_date=str(args.end_date))
    if raw.empty:
        print("Aucune donnee daily_top_proba_picks sur la periode.")
        return

    df = apply_ev_fav_band(raw, ev_min_pct=float(args.ev_min_pct), ev_max_pct=float(args.ev_max_pct))
    df_composite = enrich_composite_scores(df)

    print(f"Periode RG replay : {RG_START} -> {args.end_date} (hors 27/05)")
    print(
        f"Filtre actif : EV favori {args.ev_min_pct:.0f}-{args.ev_max_pct:.0f}% "
        f"(toggle live), puis top N / tour / jour."
    )
    print(
        f"Picks bruts {len(raw)} -> {len(df)} apres EV | "
        f"sources : {raw['capture_source'].value_counts().to_dict()}"
    )
    print("Note : majorite backfill_algo_opportunities (proxy, pas snapshot exact).")
    print(
        "Composite = priority_score sur le favori modele "
        "(Sharpe unitaire / Brier x qualite segment), meme paris que top probas."
    )

    print("\n--- Classement PROBA favori ---")
    _print_block("ATP + WTA", df, (5, 10, 15), rank_by="proba")
    for tour in ("ATP", "WTA"):
        _print_block(tour, df[df["tour"] == tour], (5, 10, 15), rank_by="proba")

    print("\n--- Classement COMPOSITE (priority_score) ---")
    _print_block("ATP + WTA", df_composite, (5, 10, 15), rank_by="composite")
    for tour in ("ATP", "WTA"):
        _print_block(tour, df_composite[df_composite["tour"] == tour], (5, 10, 15), rank_by="composite")

    print(f"\n=== ALL combine · PROBA · EV {args.ev_min_pct:.0f}-{args.ev_max_pct:.0f}% ===")
    for n in (5, 10, 15):
        sub = _select_topn(df, n, mode="all", rank_by="proba")
        flat = _flat_metrics(sub)
        sign = "+" if flat["profit_1u"] >= 0 else ""
        print(
            f"Top {n:2d}/jour ALL | settled {flat['n_settled']:3d} open {flat['n_open']:2d} | "
            f"hit {flat['hit_pct']:5.1f}% | 1u {sign}{flat['profit_1u']:.2f}u ({sign}{flat['roi_1u_pct']:.1f}%)"
        )

    print(f"\n=== ALL combine · COMPOSITE · EV {args.ev_min_pct:.0f}-{args.ev_max_pct:.0f}% ===")
    for n in (5, 10, 15):
        sub = _select_topn(df_composite, n, mode="all", rank_by="composite")
        flat = _flat_metrics(sub)
        sign = "+" if flat["profit_1u"] >= 0 else ""
        print(
            f"Top {n:2d}/jour ALL | settled {flat['n_settled']:3d} open {flat['n_open']:2d} | "
            f"hit {flat['hit_pct']:5.1f}% | 1u {sign}{flat['profit_1u']:.2f}u ({sign}{flat['roi_1u_pct']:.1f}%)"
        )


if __name__ == "__main__":
    main()
