#!/usr/bin/env python3
"""Perf globale 2026 Pack 1 (Top5 qualité) + Pack 2 (1D1P renforcé)."""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_portfolio_improvements import (
    FLAT,
    Strategy,
    _book_gap_ok,
    _conf_ok,
    _norm,
    _simulate_picks,
    run_strategy,
    select_1d1p_day,
    select_pool,
    select_top5_day,
)
from scripts.backtest_csv_pick_rows import augment_csv_pick_fields, dataframe_to_pick_rows
from scripts.backtest_staking_sim import load_and_filter_bets_csv, simulate_sequential_intraday
from scripts.bets_db import open_db, read_daily_top_proba_picks, sync_daily_top_proba_from_results
from scripts.ml_model import TennisMLModel
from scripts.simulate_top10_proba_2026 import (
    DEFAULT_EXTRA_EXCLUDE,
    DEFAULT_TOURNEY_LEVELS,
    KELLY_BASE,
    MAX_STAKE_PCT,
    _segment_calibration_key,
)

CSV = os.path.join(ROOT, "data", "backtest_2026_bets.csv")
LIVE_CUTOFF = "2026-05-18"
BR_START = 100.0

PACK1 = Strategy(
    "Pack1 Top5",
    "top5",
    rel_min=85,
    surface_cap=2,
    book_gap_max=15.0,
)
PACK2 = Strategy(
    "Pack2 1D1P",
    "1d1p",
    rel_min=90,
    conf_min_1d1p=0.55,
    book_gap_max=15.0,
)


@dataclass
class Block:
    label: str
    pack1: object
    pack2: object
    combined_flat: float
    combined_picks: int
    combined_wins: int


def _csv_rows() -> list[dict]:
    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    df = load_and_filter_bets_csv(
        CSV,
        year=2026,
        ev_min_pct=15.0,
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=list(DEFAULT_TOURNEY_LEVELS),
        extra_tournament_tokens=extra,
    )
    df = df[df["ev"].astype(float) <= 1.0].copy()
    df = df[df["p_model"].astype(float) > 0.60].copy()
    df = df[df["date"].dt.strftime("%Y-%m-%d") < LIVE_CUTOFF].copy()
    df["book_gap_pp"] = (df["p_model"].astype(float) - df["p_implied"].astype(float)).abs() * 100.0
    df["p_model_fav"] = df["p_model"].astype(float)
    df["ev_fav_pct"] = df["ev"].astype(float) * 100.0
    df["odd_fav"] = df["odd"].astype(float)
    df["calendar_date"] = df["date"].dt.strftime("%Y-%m-%d")
    df["surface"] = df["surface"].astype(str).str.lower()
    df["tour"] = df["tour"].astype(str).str.upper()
    df["match_name"] = df.apply(lambda r: f"{r['winner_name']} vs {r['loser_name']}", axis=1)
    df["settled"] = True
    df["won"] = df["won"].astype(bool)
    df = augment_csv_pick_fields(df)
    return dataframe_to_pick_rows(df)


def _csv_1d1p(rows: list[dict], strat: Strategy) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r["calendar_date"])].append(r)
    picks: list[dict] = []
    for day in sorted(by_day):
        group = by_day[day]
        cands: list[dict] = []
        for tour in ("ATP", "WTA"):
            tour_rows = sorted(
                [g for g in group if str(g.get("tour") or "").upper() == tour and _book_gap_ok(g, strat.book_gap_max)],
                key=lambda r: -float(r.get("p_model_fav") or 0),
            )
            if tour_rows:
                cands.append(tour_rows[0])
        if not cands:
            continue
        pick = max(cands, key=lambda r: (float(r.get("p_model_fav") or 0), str(r.get("tour") or "") == "ATP"))
        if _conf_ok(pick, strat.conf_min_1d1p):
            picks.append(pick)
    return picks


def _live_rows() -> list[dict]:
    db = os.path.join(ROOT, "data", "bettinghud.db")
    conn = open_db(db)
    try:
        sync_daily_top_proba_from_results(conn)
    finally:
        conn.close()
    raw = read_daily_top_proba_picks(db_path=db)
    return [_norm(r) for r in raw if str(r.get("calendar_date") or "")[:10] >= LIVE_CUTOFF]


def _tag(picks: list[dict], pack: str) -> list[dict]:
    out = []
    for p in picks:
        q = dict(p)
        q["_pack"] = pack
        out.append(q)
    return out


def _picks_to_kelly_df(picks: list[dict], ml: TennisMLModel) -> pd.DataFrame:
    rows: list[dict] = []
    for i, p in enumerate(picks):
        if p.get("settled") is False:
            continue
        rows.append(
            {
                "date": pd.to_datetime(str(p.get("calendar_date") or "")[:10]),
                "p_model": float(p.get("p_model_fav") or p.get("p_model") or 0),
                "odd": float(p.get("odd_fav") or p.get("odd") or 0),
                "won": bool(p.get("won")),
                "tour": p.get("tour"),
                "surface": p.get("surface"),
                "tournament": p.get("tournament"),
                "tourney_level": p.get("tourney_level"),
                "segment_brier": p.get("segment_brier"),
                "segment_key": p.get("segment_key"),
                "_pack": str(p.get("_pack") or "z"),
                "_proba": float(p.get("p_model_fav") or p.get("p_model") or 0),
                "_ord": i,
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["_pack_ord"] = df["_pack"].map({"1d1p": 0, "top5": 1}).fillna(2)
    df = df.sort_values(["date", "_pack_ord", "_proba", "_ord"], ascending=[True, True, False, True], kind="mergesort")
    df["segment_calibration_key"] = df.apply(
        lambda r: str(r["segment_key"]) if pd.notna(r.get("segment_key")) and str(r.get("segment_key")).strip()
        else _segment_calibration_key(r, ml),
        axis=1,
    )
    return df.drop(columns=["_pack_ord", "_proba", "_ord", "_pack", "segment_key"], errors="ignore")


def _kelly_sim(picks: list[dict], ml: TennisMLModel, *, br_start: float = BR_START) -> dict:
    df = _picks_to_kelly_df(picks, ml)
    if df.empty:
        return {"n_bets": 0, "net_profit_eur": 0.0, "bankroll_final": br_start, "roi_on_staked_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe_daily": 0.0, "total_staked_eur": 0.0, "win_rate_pct": 0.0}
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    return simulate_sequential_intraday(
        df,
        bankroll_start=br_start,
        kelly_multiplier=1.0,
        max_stake_pct=MAX_STAKE_PCT,
        daily_stake_budget_pct=100.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=KELLY_BASE,
        segment_brier_scores=seg,
        global_brier_score=glob_b,
        stake_cap_basis="liquid",
    )


def _print_kelly(label: str, picks: list[dict], ml: TennisMLModel) -> dict:
    k = _kelly_sim(picks, ml)
    print(
        f"{label:<28} n={int(k.get('n_bets') or 0):>3} "
        f"hit={float(k.get('win_rate_pct') or 0):>5.1f}% "
        f"profit={float(k.get('net_profit_eur') or 0):>+8.1f}€ "
        f"BR={float(k.get('bankroll_final') or BR_START):>7.1f}€ "
        f"ROI_vol={float(k.get('roi_on_staked_pct') or 0):>+6.1f}% "
        f"DD={float(k.get('max_drawdown_pct') or 0):>5.1f}% "
        f"Sharpe={float(k.get('sharpe_daily') or 0):>5.2f} "
        f"vol={float(k.get('total_staked_eur') or 0):>6.0f}€"
    )
    return k


def _perf_flat(picks: list[dict]) -> tuple[int, int, int, float]:
    settled = [p for p in picks if p.get("settled")]
    wins = sum(1 for p in settled if p.get("won"))
    pnl = 0.0
    for p in settled:
        odd = float(p.get("odd_fav") or p.get("odd") or 0)
        pnl += FLAT * (odd - 1.0) if p.get("won") else -FLAT
    return len(picks), len(settled), wins, pnl


def _print_perf(name: str, p1, p2) -> Block:
    _, n1, w1, flat1 = _perf_flat(p1)
    _, n2, w2, flat2 = _perf_flat(p2)
    ns = n1 + n2
    ws = w1 + w2
    flat = flat1 + flat2
    hit = (ws / ns * 100) if ns else 0
    roi = (flat / (ns * FLAT) * 100) if ns else 0
    print(f"\n=== {name} ===")
    print(f"Pack1 Top5 : {n1} paris | hit {(w1/n1*100) if n1 else 0:.1f}% | {flat1:+.0f}€")
    print(f"Pack2 1D1P  : {n2} paris | hit {(w2/n2*100) if n2 else 0:.1f}% | {flat2:+.0f}€")
    print(f"COMBINÉ     : {ns} paris | hit {hit:.1f}% | {flat:+.0f}€ | ROI {roi:+.1f}%")
    return Block(name, None, None, flat, ns, ws)


def main() -> int:
    csv_rows = _csv_rows()
    live_rows = _live_rows()

    # CSV block
    p1_csv = run_strategy(csv_rows, PACK1)
    p1_csv_picks = []
    by_day = defaultdict(list)
    for r in csv_rows:
        by_day[str(r["calendar_date"])].append(r)
    for day in sorted(by_day):
        pool = select_pool(by_day[day], rel_min=85, book_gap_max=15.0)
        p1_csv_picks.extend(select_top5_day(pool, surface_cap=2, limit=5))
    p2_csv = _csv_1d1p(csv_rows, PACK2)

    # Live block
    p2_live_picks = []
    p1_live_picks = []
    by_day_l = defaultdict(list)
    for r in live_rows:
        by_day_l[str(r["calendar_date"])[:10]].append(r)
    for day in sorted(by_day_l):
        group = by_day_l[day]
        pool = select_pool(group, rel_min=85, book_gap_max=15.0)
        p1_live_picks.extend(select_top5_day(pool, surface_cap=2, limit=5))
        p = select_1d1p_day(group, rel_min=90, conf_min=0.55, book_gap_max=15.0)
        if p:
            p2_live_picks.append(p)

    b_csv = _print_perf(f"CSV no-leak (< {LIVE_CUTOFF})", p1_csv_picks, p2_csv)
    b_live = _print_perf(f"Live prod (≥ {LIVE_CUTOFF})", p1_live_picks, p2_live_picks)

    all_p1 = _tag(p1_csv_picks, "top5") + _tag(p1_live_picks, "top5")
    all_p2 = _tag(p2_csv, "1d1p") + _tag(p2_live_picks, "1d1p")
    b_all = _print_perf("GLOBAL 2026 (CSV + Live)", p1_csv_picks + p1_live_picks, p2_csv + p2_live_picks)

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    print(f"\n=== KELLY 1/2 × Brier (cap {MAX_STAKE_PCT:.0f}% liquidité, BR départ {BR_START:.0f}€) ===")
    _print_kelly("Pack1 Top5 global", all_p1, ml)
    _print_kelly("Pack2 1D1P global", all_p2, ml)
    k_all = _print_kelly("Pack1+2 combiné", all_p1 + all_p2, ml)
    _print_kelly("CSV no-leak", _tag(p1_csv_picks, "top5") + _tag(p2_csv, "1d1p"), ml)
    _print_kelly("Live prod", _tag(p1_live_picks, "top5") + _tag(p2_live_picks, "1d1p"), ml)

    print("\n--- Par mois (Kelly Pack1+2, BR séquentielle) ---")
    combined = sorted(all_p1 + all_p2, key=lambda p: str(p.get("calendar_date") or "")[:10])
    by_m2: dict[str, list] = defaultdict(list)
    for p in combined:
        by_m2[str(p.get("calendar_date") or "")[:7]].append(p)
    br = BR_START
    for m in sorted(by_m2):
        km = _kelly_sim(by_m2[m], ml, br_start=br)
        br = float(km.get("bankroll_final") or br)
        print(
            f"{m}  n={int(km.get('n_bets') or 0):>3}  "
            f"profit={float(km.get('net_profit_eur') or 0):>+7.1f}€  "
            f"BR={br:>7.1f}€  DD={float(km.get('max_drawdown_pct') or 0):.1f}%"
        )

    # Par mois flat
    print("\n--- Par mois (flat 5€) ---")
    by_m: dict[str, list] = defaultdict(list)
    for p in all_p1 + all_p2:
        by_m[str(p.get("calendar_date") or "")[:7]].append(p)
    cum = 0.0
    for m in sorted(by_m):
        _, n, w, pnl = _perf_flat(by_m[m])
        cum += pnl
        print(f"{m}  {n:>3} paris  {pnl:>+6.0f}€  cumul {cum:>+7.0f}€")

    print(f"\n{'='*50}")
    print(f"FLAT  : {b_all.combined_picks} paris | {b_all.combined_flat:+.0f}€ | ROI {(b_all.combined_flat/(b_all.combined_picks*FLAT)*100) if b_all.combined_picks else 0:+.1f}%")
    print(
        f"KELLY : {int(k_all.get('n_bets') or 0)} paris | {float(k_all.get('net_profit_eur') or 0):+.1f}€ | "
        f"BR finale {float(k_all.get('bankroll_final') or BR_START):.1f}€ | "
        f"ROI volume {float(k_all.get('roi_on_staked_pct') or 0):+.1f}% | "
        f"DD max {float(k_all.get('max_drawdown_pct') or 0):.1f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
