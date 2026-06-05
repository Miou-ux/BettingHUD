"""
Recherche automatique de parametres live (EV min/max, top N, tri proba/EV).

Walk-forward : score calcule sur annees de validation uniquement.
Objectif par defaut : ROI 1u moyen - penalites (volume, Brier, drawdown).

Usage:
    py -3 scripts/optimize_selection_params.py
    py -3 scripts/optimize_selection_params.py --train-years 2024 --val-years 2025
    py -3 scripts/optimize_selection_params.py --quick
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
from typing import Any

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.compare_top5_proba_vs_ev_2026 import run_scenario
from scripts.ml_model import TennisMLModel

DEFAULT_EV_MINS = [5, 8, 10, 12, 15, 18, 20, 25, 30]
DEFAULT_EV_MAXS = [50, 75, 100, 125, 150]
DEFAULT_TOP_NS = [3, 5, 7, 10, 15]
DEFAULT_RANK_BY = ["proba", "ev"]
DEFAULT_P_MODEL_MINS = [0.0, 0.55, 0.60, 0.65]


def _apply_p_model_floor(df: pd.DataFrame, p_model_min: float) -> pd.DataFrame:
    if df.empty or p_model_min <= 0:
        return df
    return df[df["p_model"].astype(float) >= float(p_model_min)].reset_index(drop=True)


def evaluate_params(
    *,
    years: list[int],
    ev_min_pct: float,
    ev_max_pct: float,
    top_n: int,
    rank_by: str,
    p_model_min: float,
    ml: TennisMLModel,
    min_bets_year: int,
) -> dict[str, Any]:
    rois: list[float] = []
    hits: list[float] = []
    briers: list[float] = []
    profits: list[float] = []
    dds: list[float] = []
    bets_total = 0
    days_total = 0

    for year in years:
        path = os.path.join(ROOT, "data", f"backtest_{year}_bets.csv")
        if not os.path.isfile(path):
            continue
        r = run_scenario(
            path,
            year=year,
            mode=rank_by,
            ev_min_pct=ev_min_pct,
            ev_max_pct=ev_max_pct,
            top_n=top_n,
            ml=ml,
        )
        # Re-filtrer pool p_model si besoin (run_scenario ne l'expose pas : refaire selection legere)
        if p_model_min > 0:
            from scripts.backtest_staking_sim import load_and_filter_bets_csv
            from scripts.simulate_top10_proba_2026 import (
                DEFAULT_EXTRA_EXCLUDE,
                DEFAULT_TOURNEY_LEVELS,
                flat_stake_metrics,
                select_top_proba_per_day,
            )
            from scripts.compare_top5_proba_vs_ev_2026 import select_top_ev_per_day

            extra = [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
            df_f = load_and_filter_bets_csv(
                path,
                year=year,
                ev_min_pct=ev_min_pct,
                allowed_tours=["ATP", "WTA"],
                allowed_tourney_levels=list(DEFAULT_TOURNEY_LEVELS),
                extra_tournament_tokens=extra,
            )
            df_f = df_f[df_f["ev"].astype(float) <= ev_max_pct / 100.0]
            df_f = _apply_p_model_floor(df_f, p_model_min)
            if rank_by == "proba":
                df_sel = select_top_proba_per_day(df_f, top_n=top_n)
            else:
                df_sel = select_top_ev_per_day(df_f, top_n=top_n)
            flat = flat_stake_metrics(df_sel, br0=100.0)
            n_b = len(df_sel)
            n_d = int(df_sel["date"].nunique()) if not df_sel.empty else 0
        else:
            flat = r["flat"]
            n_b = r["bets"]
            n_d = r["days"]

        if n_b < min_bets_year:
            rois.append(-999.0)
            hits.append(0.0)
            briers.append(1.0)
            profits.append(0.0)
            dds.append(100.0)
        else:
            rois.append(float(flat["roi_pct"]))
            hits.append(float(flat["hit_pct"]))
            briers.append(float(flat["brier"]))
            profits.append(float(flat["net_units"]))
            dds.append(float(flat.get("max_drawdown_pct", 0.0)))
        bets_total += n_b
        days_total += n_d

    n_y = max(len(years), 1)
    return {
        "roi_1u_mean": sum(rois) / n_y if rois else -999.0,
        "roi_1u_min": min(rois) if rois else -999.0,
        "hit_mean": sum(hits) / n_y if hits else 0.0,
        "brier_mean": sum(briers) / n_y if briers else 1.0,
        "profit_total": sum(profits),
        "dd_1u_max": max(dds) if dds else 100.0,
        "bets_total": bets_total,
        "days_total": days_total,
    }


def composite_score(
    m: dict[str, Any],
    *,
    w_roi: float = 1.0,
    w_brier: float = 8.0,
    w_dd: float = 0.15,
    w_vol: float = 0.02,
    brier_target: float = 0.19,
    min_bets_year: int = 150,
) -> float:
    """Plus haut = mieux. Penalites si trop peu de paris ou mauvaise calibration."""
    if m["roi_1u_mean"] <= -500:
        return -1e6
    bets_per_year = m["bets_total"] / max(len(m.get("_years") or [1]), 1)
    vol_pen = max(0.0, min_bets_year - bets_per_year) * w_vol
    brier_pen = max(0.0, m["brier_mean"] - brier_target) * w_brier * 100.0
    dd_pen = max(0.0, m["dd_1u_max"] - 15.0) * w_dd
    stability_pen = max(0.0, -m["roi_1u_min"]) * 0.5
    return (
        m["roi_1u_mean"] * w_roi
        - brier_pen
        - dd_pen
        - vol_pen
        - stability_pen
    )


def run_search(
    *,
    train_years: list[int],
    val_years: list[int],
    ev_mins: list[float],
    ev_maxs: list[float],
    top_ns: list[int],
    rank_bys: list[str],
    p_model_mins: list[float],
    min_bets_year: int,
    top_k: int = 15,
) -> pd.DataFrame:
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    combos = list(
        itertools.product(ev_mins, ev_maxs, top_ns, rank_bys, p_model_mins)
    )
    rows: list[dict] = []
    print(f"Grille : {len(combos)} combinaisons | train={train_years} val={val_years}")

    for i, (ev_min, ev_max, top_n, rank_by, p_min) in enumerate(combos):
        if ev_max < ev_min:
            continue
        m_val = evaluate_params(
            years=val_years,
            ev_min_pct=ev_min,
            ev_max_pct=ev_max,
            top_n=top_n,
            rank_by=rank_by,
            p_model_min=p_min,
            ml=ml,
            min_bets_year=min_bets_year,
        )
        m_val["_years"] = val_years
        m_train = evaluate_params(
            years=train_years,
            ev_min_pct=ev_min,
            ev_max_pct=ev_max,
            top_n=top_n,
            rank_by=rank_by,
            p_model_min=p_min,
            ml=ml,
            min_bets_year=min_bets_year,
        )
        score = composite_score(m_val, min_bets_year=min_bets_year)
        rows.append(
            {
                "score": round(score, 3),
                "ev_min_pct": ev_min,
                "ev_max_pct": ev_max,
                "top_n": top_n,
                "rank_by": rank_by,
                "p_model_min": p_min,
                "val_roi_1u_mean": round(m_val["roi_1u_mean"], 2),
                "val_roi_1u_min": round(m_val["roi_1u_min"], 2),
                "val_hit_mean": round(m_val["hit_mean"], 2),
                "val_brier_mean": round(m_val["brier_mean"], 4),
                "val_profit_total": round(m_val["profit_total"], 1),
                "val_bets": m_val["bets_total"],
                "val_dd_1u_max": round(m_val["dd_1u_max"], 2),
                "train_roi_1u_mean": round(m_train["roi_1u_mean"], 2),
                "train_brier_mean": round(m_train["brier_mean"], 4),
            }
        )
        if (i + 1) % 200 == 0:
            print(f"  ... {i + 1}/{len(combos)}")

    df = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    print(f"\nTop {top_k} (score composite sur validation {val_years}):")
    print(df.head(top_k).to_string(index=False))
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description="Optimisation parametres selection live")
    ap.add_argument("--train-years", type=str, default="2024")
    ap.add_argument("--val-years", type=str, default="2025")
    ap.add_argument("--ev-mins", type=str, default=None)
    ap.add_argument("--ev-maxs", type=str, default=None)
    ap.add_argument("--top-ns", type=str, default=None)
    ap.add_argument("--rank-by", type=str, default=None, help="proba,ev ou les deux")
    ap.add_argument("--p-model-mins", type=str, default=None)
    ap.add_argument("--min-bets-year", type=int, default=150)
    ap.add_argument("--top-k", type=int, default=15)
    ap.add_argument(
        "--quick",
        action="store_true",
        help="Grille reduite pour test rapide.",
    )
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    train_years = [int(x) for x in args.train_years.split(",") if x.strip()]
    val_years = [int(x) for x in args.val_years.split(",") if x.strip()]

    if args.quick:
        ev_mins = [10, 15, 20]
        ev_maxs = [75, 100]
        top_ns = [5, 10]
        rank_bys = ["proba"]
        p_model_mins = [0.0, 0.60]
    else:
        ev_mins = (
            [float(x) for x in args.ev_mins.split(",")]
            if args.ev_mins
            else DEFAULT_EV_MINS
        )
        ev_maxs = (
            [float(x) for x in args.ev_maxs.split(",")]
            if args.ev_maxs
            else DEFAULT_EV_MAXS
        )
        top_ns = [int(x) for x in args.top_ns.split(",")] if args.top_ns else DEFAULT_TOP_NS
        rank_bys = (
            [x.strip() for x in args.rank_by.split(",")]
            if args.rank_by
            else DEFAULT_RANK_BY
        )
        p_model_mins = (
            [float(x) for x in args.p_model_mins.split(",")]
            if args.p_model_mins
            else DEFAULT_P_MODEL_MINS
        )

    df = run_search(
        train_years=train_years,
        val_years=val_years,
        ev_mins=ev_mins,
        ev_maxs=ev_maxs,
        top_ns=top_ns,
        rank_bys=rank_bys,
        p_model_mins=p_model_mins,
        min_bets_year=int(args.min_bets_year),
        top_k=int(args.top_k),
    )

    out = args.out or os.path.join(
        ROOT,
        "data",
        "reports",
        f"optimize_selection_train{train_years[0]}_val{val_years[0]}.csv",
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\nExport : {os.path.relpath(out, ROOT)}")


if __name__ == "__main__":
    main()
