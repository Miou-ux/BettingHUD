#!/usr/bin/env python3
"""Option B — replay live top 5 proba depuis algo_opportunities (v47 + cotes book).

Protocole aligné Paris du jour / Telegram :
  - EV favori 15–100 %
  - Top 5 / jour tri p_model
  - Dédupe (jour, bet_on, match_name)
  - Kelly 1/2 x Brier, cap 15 % liquidité

Compare optionnellement la même fenêtre sur backtest CSV no-leak.

Usage:
  py -3 scripts/replay_live_top5_proba.py
  py -3 scripts/replay_live_top5_proba.py --start 2026-05-18 --end 2026-05-29
  py -3 scripts/replay_live_top5_proba.py --compare-csv data/backtest_2026_bets.csv
"""
from __future__ import annotations

import argparse
import os
import sqlite3
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

DB_DEFAULT = os.path.join(ROOT, "data", "bettinghud.db")
BR_START = 100.0
EV_MIN_PCT = 15.0
EV_MAX_PCT = 100.0
TOP_N = 5


def _norm_status(x) -> str:
    s = str(x or "").lower()
    if "gagn" in s:
        return "won"
    if "perdu" in s:
        return "lost"
    if "cours" in s:
        return "open"
    return "other"


def load_algo_opportunities(
    *,
    db_path: str,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    cn = sqlite3.connect(db_path)
    q = """
        SELECT opportunity_key, match_date, detected_date, detected_ts,
               tournament, tour, surface, status,
               p_model, ev, odd_book, true_odd, real_odd, bet_on, match_name,
               segment_brier, segment_key, priority_score, snapshot_tier
        FROM algo_opportunities
        WHERE p_model IS NOT NULL AND odd_book IS NOT NULL
    """
    params: list = []
    if start:
        q += " AND match_date >= ?"
        params.append(start)
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


def compute_metrics(
    df: pd.DataFrame,
    *,
    ml: TennisMLModel,
    label: str,
    br_start: float = BR_START,
) -> dict:
    settled = df[df["settled"]].copy() if "settled" in df.columns else df[df["won"].notna()].copy()
    if "settled" not in df.columns:
        settled = df.copy()
    open_n = int((df.get("st", pd.Series()) == "open").sum()) if "st" in df.columns else 0

    out: dict = {
        "source": label,
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
                "kelly_roi_vol_pct": 0.0,
                "kelly_max_dd_pct": 0.0,
                "kelly_sharpe_j": 0.0,
                "kelly_pf": 0.0,
            }
        )
        return out

    work = settled.copy()
    work["segment_calibration_key"] = work.apply(
        lambda r: _segment_calibration_key(r, ml), axis=1
    )
    flat = flat_stake_metrics(work, br0=br_start)
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    kdf = work.sort_values(["date", "p_model"], ascending=[True, False], kind="mergesort")
    kelly = simulate_sequential_intraday(
        kdf,
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
    out.update(
        {
            "hit_pct": float(flat["hit_pct"]),
            "profit_1u": float(flat["net_units"]),
            "roi_1u_pct": float(flat["roi_pct"]),
            "brier": float(flat["brier"]),
            "kelly_roi_vol_pct": float(kelly["roi_on_staked_pct"]),
            "kelly_max_dd_pct": float(kelly["max_drawdown_pct"]),
            "kelly_sharpe_j": float(kelly["sharpe_daily"]),
            "kelly_pf": float(kelly.get("profit_factor") or 0.0),
        }
    )
    return out


def load_csv_window(
    csv_path: str,
    *,
    start: str | None,
    end: str | None,
    top_n: int,
    ev_min_pct: float,
    ev_max_pct: float,
) -> pd.DataFrame:
    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    df_f = load_and_filter_bets_csv(
        csv_path,
        year=2026,
        ev_min_pct=ev_min_pct,
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=list(DEFAULT_TOURNEY_LEVELS),
        extra_tournament_tokens=extra,
    )
    df_f = df_f[df_f["ev"].astype(float) <= ev_max_pct / 100.0].reset_index(drop=True)
    df_f["date"] = pd.to_datetime(df_f["date"], errors="coerce")
    if start:
        df_f = df_f[df_f["date"] >= pd.Timestamp(start)]
    if end:
        df_f = df_f[df_f["date"] <= pd.Timestamp(end)]
    df_sel = select_top_proba_per_day(df_f, top_n=top_n)
    df_sel = df_sel.copy()
    df_sel["settled"] = True
    return df_sel.reset_index(drop=True)


def _print_metrics(m: dict) -> None:
    print(
        f"  Paris {m['paris_total']} (settled {m['paris_settled']}, open {m['paris_open']}) "
        f"· {m['jours']} jours"
    )
    print(
        f"  1 u : hit {m['hit_pct']:.1f}% · ROI {m['roi_1u_pct']:+.1f}% · "
        f"profit {m['profit_1u']:+.1f} u · Brier {m['brier']:.3f}"
    )
    print(
        f"  Kelly 1/2 : ROI vol {m['kelly_roi_vol_pct']:+.1f}% · DD {m['kelly_max_dd_pct']:.1f}% · "
        f"Sharpe j {m['kelly_sharpe_j']:.2f} · PF {m['kelly_pf']:.2f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--start", default=None, help="match_date min (defaut: min DB)")
    ap.add_argument("--end", default=None, help="match_date max (defaut: max DB)")
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--ev-min-pct", type=float, default=EV_MIN_PCT)
    ap.add_argument("--ev-max-pct", type=float, default=EV_MAX_PCT)
    ap.add_argument(
        "--compare-csv",
        default=os.path.join(ROOT, "data", "backtest_2026_bets.csv"),
        help="CSV no-leak pour comparaison même fenêtre (vide = skip)",
    )
    ap.add_argument("--export", default=os.path.join(ROOT, "data", "reports", "replay_live_top5_proba.csv"))
    args = ap.parse_args()

    # Résoudre les statuts avant replay (sinon beaucoup de faux « En cours »).
    try:
        import sqlite3

        from scripts.bets_db import (
            sync_algo_opportunities_from_results,
            sync_daily_top_proba_from_results,
        )

        _cn = sqlite3.connect(args.db)
        sync_algo_opportunities_from_results(_cn)
        sync_daily_top_proba_from_results(_cn)
        _cn.close()
    except Exception:
        pass

    raw = load_algo_opportunities(db_path=args.db, start=args.start, end=args.end)
    if raw.empty:
        raise SystemExit(f"Aucune opportunité dans {args.db}")

    if args.start is None:
        args.start = str(raw["match_date"].min())
    if args.end is None:
        args.end = str(raw["match_date"].max())

    ml = TennisMLModel()
    ml._load_bundle_if_needed()

    pool = apply_ev_band(dedupe_opportunities(raw), ev_min_pct=args.ev_min_pct, ev_max_pct=args.ev_max_pct)
    selected = select_top_proba_per_day(pool, top_n=int(args.top_n))

    print("=" * 88)
    print("REPLAY LIVE (Option B) — algo_opportunities · v47 au moment capture")
    print(
        f"Periode {args.start} -> {args.end} · Top {args.top_n} proba/j · "
        f"EV {args.ev_min_pct:.0f}–{args.ev_max_pct:.0f} % · Kelly 1/2 x Brier"
    )
    print(f"DB : {os.path.relpath(args.db, ROOT)}")
    print(f"Brut {len(raw)} lignes -> pool EV {len(pool)} -> selection {len(selected)}")
    print("=" * 88)

    live_m = compute_metrics(selected, ml=ml, label="live_algo_opportunities")
    print("\n--- Live (algo_opportunities) ---")
    _print_metrics(live_m)

    rows = [live_m]
    if args.compare_csv and os.path.isfile(args.compare_csv):
        csv_sel = load_csv_window(
            args.compare_csv,
            start=args.start,
            end=args.end,
            top_n=int(args.top_n),
            ev_min_pct=float(args.ev_min_pct),
            ev_max_pct=float(args.ev_max_pct),
        )
        csv_m = compute_metrics(csv_sel, ml=ml, label="csv_no_leak")
        rows.append(csv_m)
        print(f"\n--- CSV no-leak ({os.path.relpath(args.compare_csv, ROOT)}) ---")
        print(f"  Meme fenetre {args.start} -> {args.end} · modele sigmoid re-entraine · cotes tennis-data")
        _print_metrics(csv_m)

        print("\n--- Écart live vs CSV (settled) ---")
        print(
            f"  Hit     : {live_m['hit_pct']:.1f}% vs {csv_m['hit_pct']:.1f}% "
            f"({live_m['hit_pct'] - csv_m['hit_pct']:+.1f} pt)"
        )
        print(
            f"  ROI 1 u : {live_m['roi_1u_pct']:+.1f}% vs {csv_m['roi_1u_pct']:+.1f}% "
            f"({live_m['roi_1u_pct'] - csv_m['roi_1u_pct']:+.1f} pt)"
        )
        print(
            f"  Brier   : {live_m['brier']:.3f} vs {csv_m['brier']:.3f} "
            f"({live_m['brier'] - csv_m['brier']:+.3f})"
        )
        print(
            f"  Paris   : {live_m['paris_settled']} settled live vs {csv_m['paris_settled']} CSV"
        )

    # Détail par jour (live settled)
    settled = selected[selected["settled"]].copy()
    if not settled.empty:
        print("\n--- PnL 1 u par jour (live, settled) ---")
        settled["pnl_1u"] = np.where(settled["won"], settled["odd"] - 1.0, -1.0)
        daily = settled.groupby(settled["date"].dt.date)["pnl_1u"].agg(["count", "sum"])
        for d, row in daily.iterrows():
            print(f"  {d} : {int(row['count'])} paris · {row['sum']:+.2f} u")

    out_dir = os.path.dirname(args.export) or "."
    os.makedirs(out_dir, exist_ok=True)
    pd.DataFrame(rows).to_csv(args.export, index=False, encoding="utf-8-sig")
    bets_out = args.export.replace(".csv", "_bets.csv")
    cols = [
        "match_date", "tournament", "tour", "bet_on", "match_name",
        "p_model", "ev_pct", "odd", "st", "won",
    ]
    selected.loc[:, [c for c in cols if c in selected.columns]].to_csv(
        bets_out, index=False, encoding="utf-8-sig"
    )
    print(f"\nExport résumé : {os.path.relpath(args.export, ROOT)}")
    print(f"Export paris  : {os.path.relpath(bets_out, ROOT)}")


if __name__ == "__main__":
    main()
