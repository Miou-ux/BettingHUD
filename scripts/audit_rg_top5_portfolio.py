"""Audit Roland-Garros : top N/jour sur algo_opportunities vs paris réels."""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import simulate_sequential_intraday
from scripts.ml_model import TennisMLModel

RG_START = "2026-05-18"
RG_MAIN_DRAW_START = "2026-05-25"
DB = os.path.join(ROOT, "data", "bettinghud.db")


def _norm_status(x) -> str:
    s = str(x or "").lower()
    if "gagn" in s:
        return "won"
    if "perdu" in s:
        return "lost"
    if "cours" in s:
        return "open"
    return "other"


def _load_rg_opportunities(*, start: str = RG_START, ev_band: bool = True) -> pd.DataFrame:
    cn = sqlite3.connect(DB)
    df = pd.read_sql_query(
        """
        SELECT opportunity_key, match_date, detected_date, tournament, tour, status,
               p_model, ev, odd_book, true_odd, bet_on, match_name,
               theoretical_profit, segment_brier, segment_key
        FROM algo_opportunities
        WHERE (
            lower(tournament) LIKE '%roland%'
            OR lower(tournament) LIKE '%garros%'
            OR lower(tournament) LIKE '%french%'
        )
        AND match_date >= ?
        """,
        cn,
        params=(start,),
    )
    cn.close()
    if ev_band:
        df["ev_pct"] = pd.to_numeric(df["ev"], errors="coerce") * 100.0
        df = df[(df["ev_pct"] >= 15.0) & (df["ev_pct"] <= 100.0)].copy()
    df["p_model"] = pd.to_numeric(df["p_model"], errors="coerce")
    df["odd_book"] = pd.to_numeric(df["odd_book"], errors="coerce")
    df["date"] = pd.to_datetime(df["match_date"], errors="coerce")
    df = df.dropna(subset=["date", "p_model", "odd_book"])
    df = df[df["odd_book"] > 1.0]
    df = df.sort_values(["date", "p_model"], ascending=[True, False], kind="mergesort")
    df["_rank"] = df.groupby("date", sort=False).cumcount() + 1
    df["st"] = df["status"].map(_norm_status)
    df["won"] = df["st"] == "won"
    return df


def _metrics_settled(sub: pd.DataFrame, *, br_start: float = 100.0) -> dict:
    settled = sub[sub["st"].isin(["won", "lost"])].copy()
    if settled.empty:
        return {"n": 0, "hit_pct": 0.0, "profit_1u": 0.0, "roi_1u_pct": 0.0}
    settled["odd"] = settled["odd_book"]
    pnl = np.where(settled["won"], settled["odd"] - 1.0, -1.0)
    n = len(settled)
    out = {
        "n": n,
        "hit_pct": float(settled["won"].mean() * 100.0),
        "profit_1u": float(pnl.sum()),
        "roi_1u_pct": float(pnl.sum() / n * 100.0),
        "open": int((sub["st"] == "open").sum()),
    }
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    kdf = settled.copy()
    kdf["segment_calibration_key"] = kdf["segment_key"].fillna("").astype(str)
    kelly = simulate_sequential_intraday(
        kdf,
        bankroll_start=float(br_start),
        kelly_multiplier=1.0,
        max_stake_pct=15.0,
        daily_stake_budget_pct=100.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=0.5,
        segment_brier_scores=seg,
        global_brier_score=glob_b,
        stake_cap_basis="liquid",
    )
    out["kelly_profit"] = float(kelly["net_profit_eur"])
    out["kelly_br_final"] = float(kelly["bankroll_final"])
    out["kelly_roi_vol_pct"] = float(kelly["roi_on_staked_pct"])
    out["kelly_max_dd_pct"] = float(kelly["max_drawdown_pct"])
    return out


def main() -> None:
    for label, start, ev_band in [
        ("RG dès 18/05 · EV 15-100%", RG_START, True),
        ("Tableau principal dès 25/05 · EV 15-100%", RG_MAIN_DRAW_START, True),
        ("RG dès 18/05 · sans filtre EV", RG_START, False),
    ]:
        df = _load_rg_opportunities(start=start, ev_band=ev_band)
        print(f"\n=== {label} ===")
        print(f"Opportunités : {len(df)} · {df['date'].nunique()} jours")
        m5 = _metrics_settled(df[df["_rank"] <= 5])
        print(
            f"Top 5 terminés : n={m5['n']} (encore ouverts={m5.get('open',0)}) "
            f"hit={m5['hit_pct']:.1f}% profit_1u={m5['profit_1u']:+.2f}u "
            f"Kelly profit={m5.get('kelly_profit',0):+.1f}€ BR={m5.get('kelly_br_final',100):.1f}€"
        )
        for tour in ["ATP", "WTA"]:
            s = df[(df["_rank"] <= 5) & (df["tour"] == tour)]
            mt = _metrics_settled(s)
            print(
                f"  {tour} top5: n={mt['n']} hit={mt['hit_pct']:.1f}% "
                f"profit_1u={mt['profit_1u']:+.2f}u"
            )

    df = _load_rg_opportunities(start=RG_START, ev_band=True)
    print("\n=== Détail · RG dès 18/05 · EV 15-100% ===")

    rows = []
    for top_n in [5, 10, 15]:
        sub = df[df["_rank"] <= top_n]
        for tour in ["ALL", "ATP", "WTA"]:
            s = sub if tour == "ALL" else sub[sub["tour"] == tour]
            m = _metrics_settled(s)
            rows.append({"Top": top_n, "Circuit": tour, **m})
    tbl = pd.DataFrame(rows)
    print("\n--- Top N · paris terminés (1u) + Kelly ---")
    print(
        tbl.to_string(
            index=False,
            columns=[
                "Top",
                "Circuit",
                "n",
                "open",
                "hit_pct",
                "profit_1u",
                "roi_1u_pct",
                "kelly_profit",
                "kelly_br_final",
                "kelly_roi_vol_pct",
                "kelly_max_dd_pct",
            ],
            float_format=lambda x: f"{x:.1f}",
        )
    )

    top5 = df[df["_rank"] <= 5]
    settled5 = top5[top5["st"].isin(["won", "lost"])]
    daily = settled5.groupby(settled5["date"].dt.date).apply(
        lambda g: float(np.where(g["won"], g["odd_book"] - 1.0, -1.0).sum())
    )
    print(f"\nTop 5 · PnL 1u cumulé (terminés) : {daily.sum():+.2f}u")
    print("Meilleurs jours 1u:", daily.nlargest(3).to_dict())
    print("Pires jours 1u:", daily.nsmallest(3).to_dict())

    cn = sqlite3.connect(DB)
    bets = pd.read_sql_query(
        """
        SELECT status, stake, profit, tour, match_date, p_model, ev_at_bet
        FROM user_bets
        WHERE (
            lower(tournament) LIKE '%roland%'
            OR lower(tournament) LIKE '%garros%'
            OR lower(tournament) LIKE '%french%'
        )
        AND match_date >= ?
        """,
        cn,
        params=(RG_START,),
    )
    cn.close()
    bets["st"] = bets["status"].map(_norm_status)
    bs = bets[bets["st"].isin(["won", "lost"])]
    stake = float(bs["stake"].sum())
    profit = float(bs["profit"].sum())
    print("\n--- Paris RÉELS (user_bets, ce que tu as joué) ---")
    print(f"Paris terminés : {len(bs)} | En cours : {int((bets['st']=='open').sum())}")
    print(f"Mises : {stake:.2f} € | PnL : {profit:+.2f} € | ROI : {profit/stake*100 if stake else 0:+.1f}%")


if __name__ == "__main__":
    main()
