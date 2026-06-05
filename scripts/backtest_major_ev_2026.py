#!/usr/bin/env python3
"""Backtest 2026 — tournois majeurs (main draw 250+), EV +15 % → +200 %.

Aligné Paris du jour / ``/jourmajor`` : ATP & WTA, hors Challenger / ITF / UTR.

Pipeline no-leak :
  1. ``backtest_2026.py`` génère le CSV (modèle entraîné avant 2026-01-01).
  2. Filtre tournois majeurs + bande EV [15 %, 200 %].
  3. Scénarios : tous les paris EV+, Top 5 / Top 10 proba par jour.
  4. Métriques mise fixe 1 u + Kelly ½ × Brier (cap 15 %).

Usage :
  py -3 scripts/backtest_major_ev_2026.py
  py -3 scripts/backtest_major_ev_2026.py --skip-backtest --csv data/backtest_2026_bets.csv
  py -3 scripts/backtest_major_ev_2026.py --regen-csv
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Any

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import (  # noqa: E402
    load_and_filter_bets_csv,
    simulate_sequential_intraday,
)
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key  # noqa: E402
from scripts.simulate_top10_proba_2026 import (  # noqa: E402
    DEFAULT_EXTRA_EXCLUDE,
    KELLY_BASE,
    MAX_STAKE_PCT,
    flat_stake_metrics,
    select_top_proba_per_day,
)
from scripts.tournament_tier import (  # noqa: E402
    is_major_atp_wta_by_name,
    is_major_tournament_match,
)

YEAR_DEFAULT = 2026
EV_MIN_PCT = 15.0
EV_MAX_PCT = 200.0
BR_START = 100.0
CSV_DEFAULT = os.path.join(ROOT, "data", f"backtest_{YEAR_DEFAULT}_bets.csv")
REPORT_DIR = os.path.join(ROOT, "data", "reports")
BETS_OUT = os.path.join(REPORT_DIR, f"backtest_major_ev_{YEAR_DEFAULT}_bets.csv")
SUMMARY_OUT = os.path.join(REPORT_DIR, f"backtest_major_ev_{YEAR_DEFAULT}_summary.csv")
BUCKETS_OUT = os.path.join(REPORT_DIR, f"backtest_major_ev_{YEAR_DEFAULT}_ev_buckets.csv")

_MINOR_NAME_TOKENS = (
    "challenger",
    "itf",
    "utr",
    "futures",
    "future",
    "m15",
    "m25",
    "w15",
    "w25",
    "w60",
    "w80",
    "w100",
)

# Niveaux tennis-data ≈ main draw 250+ (G / M / A dashboard).
_MAJOR_LEVEL_CODES = frozenset({"G", "M", "A", "PM", "P", "500", "250"})


def _row_as_match(row: pd.Series) -> dict[str, Any]:
    return {
        "category": row.get("tour"),
        "tour": row.get("tour"),
        "tournament": row.get("tournament"),
        "tourney_level": row.get("tourney_level"),
        "tournament_url": "",
        "tourney_winner_points": None,
    }


def is_major_bet_row(row: pd.Series) -> bool:
    """True si le pari appartient au pool majeurs (250+ main draw)."""
    tour = str(row.get("tour") or "").strip().upper()
    if tour not in {"ATP", "WTA"}:
        return False
    tname = str(row.get("tournament") or "").lower()
    if any(tok in tname for tok in _MINOR_NAME_TOKENS):
        return False
    m = _row_as_match(row)
    if is_major_tournament_match(m):
        return True
    lvl = str(row.get("tourney_level") or "").strip().upper()
    if lvl in _MAJOR_LEVEL_CODES:
        return is_major_atp_wta_by_name(tour, row.get("tournament"))
    return False


def filter_major_tournaments_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    mask = df.apply(is_major_bet_row, axis=1)
    return df.loc[mask].reset_index(drop=True)


def filter_ev_band(df: pd.DataFrame, *, ev_min_pct: float, ev_max_pct: float) -> pd.DataFrame:
    if df.empty or "ev" not in df.columns:
        return df.iloc[0:0].copy()
    lo = float(ev_min_pct) / 100.0
    hi = float(ev_max_pct) / 100.0
    ev = df["ev"].astype(float)
    return df.loc[(ev >= lo) & (ev <= hi)].reset_index(drop=True)


def _segment_key(row: pd.Series, ml: TennisMLModel) -> str:
    return resolve_match_brier_segment_key(
        ml,
        tour=row.get("tour"),
        surface=row.get("surface"),
        tournament=row.get("tournament"),
        tourney_level=row.get("tourney_level"),
    )


def _attach_segment_keys(df: pd.DataFrame, ml: TennisMLModel) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["segment_calibration_key"] = out.apply(lambda r: _segment_key(r, ml), axis=1)
    return out


def _kelly_metrics(df: pd.DataFrame, ml: TennisMLModel, *, br_start: float) -> dict:
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    return simulate_sequential_intraday(
        df,
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


def run_scenario(
    df_pool: pd.DataFrame,
    *,
    label: str,
    mode: str,
    top_n: int,
    ml: TennisMLModel,
    br_start: float,
) -> dict[str, Any]:
    if mode == "all":
        df_sel = df_pool.copy()
    elif mode == "top_proba":
        df_sel = select_top_proba_per_day(df_pool, top_n=int(top_n))
    else:
        raise ValueError(f"mode inconnu: {mode}")

    df_sel = _attach_segment_keys(df_sel, ml)
    flat = flat_stake_metrics(df_sel, br0=float(br_start))
    kelly = _kelly_metrics(df_sel, ml, br_start=float(br_start))

    return {
        "scenario": label,
        "mode": mode,
        "top_n": top_n if mode == "top_proba" else None,
        "pool_bets": len(df_pool),
        "pool_days": int(df_pool["date"].nunique()) if not df_pool.empty else 0,
        "bets": len(df_sel),
        "days": int(df_sel["date"].nunique()) if not df_sel.empty else 0,
        "hit_pct": round(flat["hit_pct"], 2),
        "roi_1u_pct": round(flat["roi_pct"], 2),
        "profit_1u": round(flat["net_units"], 2),
        "br_1u_final": round(flat["bankroll_final"], 2),
        "dd_1u_pct": round(flat["max_drawdown_pct"], 2),
        "brier": round(flat["brier"], 4),
        "roi_kelly_pct": round(kelly["roi_on_staked_pct"], 2),
        "br_kelly_final": round(kelly["bankroll_final"], 2),
        "br_kelly_mult": round(kelly["bankroll_final"] / float(br_start), 3),
        "dd_kelly_pct": round(kelly["max_drawdown_pct"], 2),
        "sharpe_daily": round(float(kelly.get("sharpe_daily") or 0), 3),
        "profit_factor": round(float(kelly.get("profit_factor") or 0), 3),
        "df_selected": df_sel,
    }


def ev_bucket_report(df: pd.DataFrame) -> pd.DataFrame:
    """ROI / hit par tranche EV (%, bornes incluses à droite sauf dernière)."""
    if df.empty:
        return pd.DataFrame()
    bins = [
        (15, 30, "15-30"),
        (30, 50, "30-50"),
        (50, 75, "50-75"),
        (75, 100, "75-100"),
        (100, 150, "100-150"),
        (150, 200, "150-200"),
    ]
    rows: list[dict] = []
    ev_pct = df["ev"].astype(float) * 100.0
    for lo, hi, label in bins:
        sub = df.loc[(ev_pct >= lo) & (ev_pct <= hi)]
        if sub.empty:
            rows.append({"ev_bucket_pct": label, "n_bets": 0})
            continue
        rets = np.where(sub["won"].astype(bool), sub["odd"].astype(float) - 1.0, -1.0)
        n = len(sub)
        rows.append(
            {
                "ev_bucket_pct": label,
                "n_bets": n,
                "hit_pct": round(100.0 * (rets > 0).sum() / n, 1),
                "roi_1u_pct": round(float(rets.sum()) / n * 100.0, 2),
                "avg_odd": round(float(sub["odd"].mean()), 2),
                "avg_ev_pct": round(float(ev_pct.loc[sub.index].mean()), 1),
            }
        )
    return pd.DataFrame(rows)


def ensure_backtest_csv(
    csv_path: str,
    *,
    year: int,
    ev_min_frac: float,
    ev_max_frac: float,
    regen: bool,
) -> str:
    if os.path.isfile(csv_path) and not regen:
        return csv_path
    script = os.path.join(ROOT, "scripts", "backtest_2026.py")
    cmd = [
        sys.executable,
        script,
        "--year",
        str(year),
        "--ev-min",
        str(ev_min_frac),
        "--ev-max",
        str(ev_max_frac),
        "--out",
        csv_path,
    ]
    print(f"[backtest] Génération no-leak : {' '.join(cmd)}")
    subprocess.run(cmd, cwd=ROOT, check=True)
    return csv_path


def load_major_ev_pool(
    csv_path: str,
    *,
    year: int,
    ev_min_pct: float,
    ev_max_pct: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Retourne (df brut année, pool majeurs + EV band)."""
    extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    df_year = load_and_filter_bets_csv(
        csv_path,
        year=int(year),
        ev_min_pct=None,
        allowed_tours=["ATP", "WTA"],
    )
    n_raw = len(df_year)
    df_major = filter_major_tournaments_df(df_year)
    df_pool = filter_ev_band(df_major, ev_min_pct=ev_min_pct, ev_max_pct=ev_max_pct)
    print(
        f"  Filtre majeurs : {n_raw} -> {len(df_major)} paris "
        f"({len(df_major) / max(n_raw, 1) * 100:.1f} % du brut {year})"
    )
    print(
        f"  Filtre EV {ev_min_pct:.0f}-{ev_max_pct:.0f} % : "
        f"{len(df_pool)} paris sur {int(df_pool['date'].nunique()) if not df_pool.empty else 0} jours"
    )
    if not df_major.empty and ev_max_pct > 100.0:
        max_seen = float(df_major["ev"].astype(float).max()) * 100.0
        if max_seen < ev_max_pct - 0.5:
            print(
                f"  [WARN] EV max observee dans le CSV : {max_seen:.1f} % — "
                f"lancer sans --skip-backtest ou avec --regen-csv pour couvrir jusqu'a {ev_max_pct:.0f} %."
            )
    return df_year, df_pool


def main() -> int:
    ap = argparse.ArgumentParser(
        description=f"Backtest {YEAR_DEFAULT} majeurs 250+ · EV {EV_MIN_PCT}-{EV_MAX_PCT} %"
    )
    ap.add_argument("--year", type=int, default=YEAR_DEFAULT)
    ap.add_argument("--csv", type=str, default=CSV_DEFAULT)
    ap.add_argument("--skip-backtest", action="store_true", help="Ne pas regénérer le CSV source")
    ap.add_argument(
        "--regen-csv",
        action="store_true",
        help="Forcer backtest_2026.py (EV min 15 %, max 200 %)",
    )
    ap.add_argument("--ev-min-pct", type=float, default=EV_MIN_PCT)
    ap.add_argument("--ev-max-pct", type=float, default=EV_MAX_PCT)
    ap.add_argument("--br-start", type=float, default=BR_START)
    ap.add_argument("--out-bets", type=str, default=BETS_OUT)
    ap.add_argument("--out-summary", type=str, default=SUMMARY_OUT)
    ap.add_argument("--out-buckets", type=str, default=BUCKETS_OUT)
    args = ap.parse_args()

    year = int(args.year)
    csv_path = args.csv or os.path.join(ROOT, "data", f"backtest_{year}_bets.csv")
    ev_min = float(args.ev_min_pct)
    ev_max = float(args.ev_max_pct)

    if not args.skip_backtest:
        ensure_backtest_csv(
            csv_path,
            year=year,
            ev_min_frac=ev_min / 100.0,
            ev_max_frac=ev_max / 100.0,
            regen=bool(args.regen_csv),
        )
    if not os.path.isfile(csv_path):
        print(f"CSV introuvable : {csv_path}", file=sys.stderr)
        return 1

    print("=" * 78)
    print(f"BACKTEST MAJEURS {year} — main draw ATP/WTA 250+")
    print(f"  EV favori : +{ev_min:.0f} % -> +{ev_max:.0f} %")
    print(f"  Source    : {csv_path}")
    print("=" * 78)

    _, df_pool = load_major_ev_pool(csv_path, year=year, ev_min_pct=ev_min, ev_max_pct=ev_max)
    if df_pool.empty:
        print("Aucun pari dans le pool — vérifier CSV et filtres.")
        return 1

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    scenarios = [
        ("Tous paris EV+ (majeurs)", "all", 0),
        ("Top 5 proba / jour", "top_proba", 5),
        ("Top 10 proba / jour", "top_proba", 10),
    ]

    results: list[dict] = []
    export_bets: pd.DataFrame | None = None
    for label, mode, top_n in scenarios:
        r = run_scenario(
            df_pool,
            label=label,
            mode=mode,
            top_n=top_n,
            ml=ml,
            br_start=float(args.br_start),
        )
        results.append({k: v for k, v in r.items() if k != "df_selected"})
        if mode == "top_proba" and top_n == 5:
            export_bets = r["df_selected"]
        print(f"\n--- {label} ---")
        print(f"  Paris : {r['bets']} ({r['days']} jours) · pool {r['pool_bets']}")
        print(
            f"  1u    : hit {r['hit_pct']:.1f} % · ROI {r['roi_1u_pct']:+.2f} % · "
            f"BR {r['br_1u_final']:.1f} · DD {r['dd_1u_pct']:.1f} %"
        )
        print(
            f"  Kelly : ROI {r['roi_kelly_pct']:+.2f} % · BR ×{r['br_kelly_mult']:.2f} · "
            f"Sharpe j. {r['sharpe_daily']:.2f}"
        )

    buckets = ev_bucket_report(df_pool)
    print("\n--- Répartition par bucket EV (pool majeurs, tous paris) ---")
    if buckets.empty:
        print("  (vide)")
    else:
        print(buckets.to_string(index=False))

    os.makedirs(os.path.dirname(args.out_summary) or REPORT_DIR, exist_ok=True)
    pd.DataFrame(results).to_csv(args.out_summary, index=False, encoding="utf-8")
    buckets.to_csv(args.out_buckets, index=False, encoding="utf-8")
    if export_bets is not None and not export_bets.empty:
        export_bets.to_csv(args.out_bets, index=False, encoding="utf-8")
    else:
        df_pool.head(0).to_csv(args.out_bets, index=False, encoding="utf-8")

    print("\nExports :")
    print(f"  {args.out_summary}")
    print(f"  {args.out_buckets}")
    print(f"  {args.out_bets} (Top 5 proba / jour)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
