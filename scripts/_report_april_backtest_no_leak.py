#!/usr/bin/env python3
"""Bilan mensuel backtest no-leak (CSV backtest_2026.py) — Top1 / Top5 / 1D1P."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.backtest_staking_sim import (  # noqa: E402
    load_and_filter_bets_csv,
    simulate_sequential_intraday,
)
from scripts.bilan_top5_1d1p import BANKROLL_START, summarize_picks  # noqa: E402
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key  # noqa: E402
from scripts.simulate_top10_proba_2026 import (  # noqa: E402
    DEFAULT_EXTRA_EXCLUDE,
    DEFAULT_TOURNEY_LEVELS,
    KELLY_BASE,
    MAX_STAKE_PCT,
    _segment_calibration_key,
    flat_stake_metrics,
    select_top_proba_per_day,
)

EV_MIN = 15.0
EV_MAX = 100.0
MIN_PROBA_TOP5 = 0.60
MARGIN = 0.04


def _impute_other_odd(known_odd: float) -> float:
    inv_other = max(1e-6, 1.0 + MARGIN - 1.0 / float(known_odd))
    return 1.0 / inv_other


def enrich_favorite_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Une ligne par match : favori modèle, cote favori, résultat favori."""
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        pw = float(r["global_p1_prob"])
        pl = 1.0 - pw
        fav_is_winner = pw >= pl
        p_fav = max(pw, pl)
        fav_won = bool(fav_is_winner)
        side_winner = str(r["side"]).upper() == "WINNER"
        odd_bet = float(r["odd"])
        if fav_is_winner:
            odd_fav = odd_bet if side_winner else _impute_other_odd(odd_bet)
            fav_player = str(r["winner_name"])
            underdog = str(r["loser_name"])
        else:
            odd_fav = odd_bet if not side_winner else _impute_other_odd(odd_bet)
            fav_player = str(r["loser_name"])
            underdog = str(r["winner_name"])
        ev_fav = p_fav * odd_fav - 1.0
        rows.append(
            {
                "calendar_date": pd.Timestamp(r["date"]).strftime("%Y-%m-%d"),
                "date": r["date"],
                "tour": str(r["tour"]).upper(),
                "match_name": f"{r['winner_name']} vs {r['loser_name']}",
                "fav_player": fav_player,
                "underdog_player": underdog,
                "p_model_fav": p_fav,
                "odd_fav": odd_fav,
                "ev_fav_pct": ev_fav * 100.0,
                "ev": ev_fav,
                "segment_brier": float(r.get("segment_brier") or 0.25),
                "segment_key": str(r.get("segment_key") or ""),
                "status": "Gagné" if fav_won else "Perdu",
                "fav_won": fav_won,
                "won": fav_won,
                "p_model": p_fav,
                "odd": odd_fav,
                "tournament": r.get("tournament"),
                "surface": r.get("surface"),
                "tourney_level": r.get("tourney_level"),
            }
        )
    return pd.DataFrame(rows)


def select_top1_per_tour(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work = work.sort_values(
        ["calendar_date", "tour", "p_model_fav"],
        ascending=[True, True, False],
        kind="mergesort",
    )
    return work.groupby(["calendar_date", "tour"], sort=False, as_index=False).first()


def select_top5_telegram(df: pd.DataFrame) -> pd.DataFrame:
    work = df[
        (df["p_model_fav"] > MIN_PROBA_TOP5)
        & (df["ev_fav_pct"] >= EV_MIN)
        & (df["ev_fav_pct"] <= EV_MAX)
    ].copy()
    work = work.sort_values(
        ["calendar_date", "p_model_fav"],
        ascending=[True, False],
        kind="mergesort",
    )
    work["_rank"] = work.groupby("calendar_date", sort=False).cumcount() + 1
    return work.loc[work["_rank"] <= 5].drop(columns=["_rank"])


def select_1d1p(df: pd.DataFrame) -> pd.DataFrame:
    picks: list[dict[str, Any]] = []
    for day, g in df.groupby("calendar_date", sort=True):
        circuit: list[dict[str, Any]] = []
        for tour in ("ATP", "WTA"):
            tour_rows = g[g["tour"] == tour].sort_values("p_model_fav", ascending=False)
            for _, row in tour_rows.iterrows():
                if EV_MIN <= float(row["ev_fav_pct"]) <= EV_MAX:
                    circuit.append(row.to_dict())
                    break
        if not circuit:
            continue
        best = max(
            circuit,
            key=lambda r: (float(r["p_model_fav"]), str(r.get("tour") or "").upper() == "ATP"),
        )
        picks.append(best)
    return pd.DataFrame(picks)


def df_to_pick_dicts(df: pd.DataFrame) -> list[dict[str, Any]]:
    from scripts.bets_db import _algo_kelly_stake_frac, _algo_profit_for_status

    out: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        row = r.to_dict()
        stake = _algo_kelly_stake_frac(
            row.get("p_model_fav"),
            row.get("odd_fav"),
            row.get("segment_brier"),
        )
        row["theoretical_stake_frac"] = stake
        row["theoretical_profit"] = _algo_profit_for_status(
            str(row.get("status") or ""),
            row.get("odd_fav"),
            stake,
        )
        out.append(row)
    return out


def kelly_intraday(df: pd.DataFrame, ml: TennisMLModel, *, br_start: float) -> dict:
    if df.empty:
        return {"bankroll_final": br_start, "roi_pct": 0.0, "max_drawdown_pct": 0.0}
    work = df.copy()
    work["segment_calibration_key"] = work.apply(
        lambda r: resolve_match_brier_segment_key(
            ml,
            tour=r.get("tour"),
            surface=r.get("surface"),
            tournament=r.get("tournament"),
            tourney_level=r.get("tourney_level"),
        ),
        axis=1,
    )
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    return simulate_sequential_intraday(
        work,
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


def brier_from_picks(picks: list[dict[str, Any]]) -> tuple[float | None, int]:
    xs = []
    for p in picks:
        st = str(p.get("status") or "").lower()
        if "gagn" not in st and "perdu" not in st:
            continue
        y = 1.0 if "gagn" in st else 0.0
        xs.append((float(p["p_model_fav"]), y))
    if not xs:
        return None, 0
    return sum((a - b) ** 2 for a, b in xs) / len(xs), len(xs)


def print_block(label: str, picks: list[dict[str, Any]], *, br_start: float, kelly: dict) -> None:
    s = summarize_picks(picks, bankroll_start=br_start, label=label)
    b, n_b = brier_from_picks(picks)
    print(f"\n## {label}")
    print(
        f"  Picks: {s['n_picks']} | G {s['n_won']} · P {s['n_lost']} · "
        f"void {s['n_void']} · open {s['n_open']}"
    )
    print(
        f"  Hit: {s['hit_pct']}% | BR {s['bankroll_final']:.2f} € "
        f"({s['growth_pct']:+.1f}%) | ROI mises {s['roi_staked_pct']:+.1f}%"
    )
    print(f"  Max DD: {s['max_drawdown_pct']}% | Mises cumulées: {s['total_staked_eur']:.2f} €")
    print(
        f"  (réf. intraday liquid: BR {kelly.get('bankroll_final', br_start):.2f} €, "
        f"DD {kelly.get('max_drawdown_pct', 0):.1f}%)"
    )
    if b is not None:
        print(f"  Brier: {b:.4f} (n={n_b})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/backtest_2026_bets.csv")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--month", type=int, default=4)
    ap.add_argument("--br-start", type=float, default=BANKROLL_START)
    args = ap.parse_args()

    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    df_f = load_and_filter_bets_csv(
        args.csv,
        year=args.year,
        ev_min_pct=0.0,
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=list(DEFAULT_TOURNEY_LEVELS),
        extra_tournament_tokens=extra,
    )
    df_f["date"] = pd.to_datetime(df_f["date"])
    month_df = df_f[df_f["date"].dt.month == int(args.month)].copy()
    fav = enrich_favorite_rows(month_df)

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    month_names = ["", "jan", "fév", "mar", "avr", "mai", "juin", "juil", "aoû", "sep", "oct", "nov", "déc"]
    mname = month_names[int(args.month)]
    print(f"=== Backtest no-leak — {mname} {args.year} ===")
    print(f"CSV: {args.csv} | matchs G/M/A: {len(fav)} | cutoff train < {args.year}-01-01")
    print("Kelly: ½×Brier · BR départ 100 € · cotes tennis-data")

    top1 = select_top1_per_tour(fav)
    top5 = select_top5_telegram(fav)
    d1 = select_1d1p(fav)

    for label, part in [
        ("Top 1 (meilleure proba / jour / circuit)", top1),
        ("Top 5 Telegram (proba>60 %, EV 15–100 %)", top5),
        ("1 Day 1 Pick", d1),
    ]:
        picks = df_to_pick_dicts(part)
        print_block(label, picks, br_start=args.br_start, kelly=kelly_intraday(part, ml, br_start=args.br_start))

    # Référence: top 1 proba/jour global (sans split tour) sur pool EV>=15
    pool_ev = fav[(fav["ev_fav_pct"] >= EV_MIN) & (fav["ev_fav_pct"] <= EV_MAX)]
    ref = select_top_proba_per_day(pool_ev, top_n=1)
    if not ref.empty:
        picks = df_to_pick_dicts(ref)
        print_block(
            "Réf. Top 1 proba/jour (pool EV 15–100 %, tous circuits confondus)",
            picks,
            br_start=args.br_start,
            kelly=kelly_intraday(ref, ml, br_start=args.br_start),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
