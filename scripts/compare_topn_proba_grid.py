"""Grille Top N proba/jour (EV 15-100 %, Kelly 1/2 x Brier) sur plusieurs années.

Usage:
    py -3 scripts/compare_topn_proba_grid.py
    py -3 scripts/compare_topn_proba_grid.py --years 2024,2025,2026 --top-ns 5,6,7,8,9,10
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.simulate_top10_proba_2026 import (  # noqa: E402
    DEFAULT_EV_MAX_PCT,
    DEFAULT_EV_MIN_PCT,
    run_year_projection,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=str, default="2024,2025,2026")
    ap.add_argument("--top-ns", type=str, default="5,6,7,8,9,10")
    ap.add_argument("--ev-min-pct", type=float, default=DEFAULT_EV_MIN_PCT)
    ap.add_argument(
        "--ev-max-pct",
        type=float,
        default=DEFAULT_EV_MAX_PCT,
        help="EV max inclus (défaut 100). Valeur négative = pas de plafond.",
    )
    ap.add_argument("--br-start", type=float, default=100.0)
    args = ap.parse_args()

    years = [int(y.strip()) for y in args.years.split(",") if y.strip()]
    top_ns = [int(n.strip()) for n in args.top_ns.split(",") if n.strip()]
    ev_max = float(args.ev_max_pct) if float(args.ev_max_pct) >= 0 else None

    from scripts.ml_model import TennisMLModel

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    rows: list[dict] = []
    for y in years:
        for top_n in top_ns:
            try:
                r = run_year_projection(
                    y,
                    top_n=top_n,
                    ev_min_pct=float(args.ev_min_pct),
                    ev_max_pct=ev_max,
                    br_start=float(args.br_start),
                    ml=ml,
                )
            except FileNotFoundError as e:
                print(f"[WARN] {e}")
                continue
            f, k = r["flat"], r["kelly"]
            rows.append(
                {
                    "annee": y,
                    "top_n": top_n,
                    "paris": r["n_bets"],
                    "jours": r["n_days"],
                    "hit_pct": round(f["hit_pct"], 1),
                    "roi_1u_pct": round(f["roi_pct"], 2),
                    "profit_1u": round(f["net_units"], 1),
                    "dd_1u_pct": round(f["max_drawdown_pct"], 1),
                    "brier": round(f["brier"], 3),
                    "roi_kelly_vol_pct": round(k["roi_on_staked_pct"], 2),
                    "dd_kelly_pct": round(k["max_drawdown_pct"], 1),
                    "sharpe_j": round(k["sharpe_daily"], 2),
                    "pf": round(float(k.get("profit_factor") or 0), 2),
                }
            )

    if not rows:
        raise SystemExit("Aucun résultat.")

    tbl = pd.DataFrame(rows)

    print("=" * 88)
    print(
        f"Grille Top N proba/jour · EV {args.ev_min_pct:.0f}"
        + (f"-{args.ev_max_pct:.0f}" if ev_max is not None else "")
        + " % · Kelly 1/2 x Brier · cap 15 % · no-leak"
    )
    print("=" * 88)

    for y in years:
        sub = tbl[tbl["annee"] == y]
        if sub.empty:
            continue
        print(f"\n--- {y} ---")
        print(
            sub[
                [
                    "top_n",
                    "paris",
                    "jours",
                    "hit_pct",
                    "roi_1u_pct",
                    "profit_1u",
                    "dd_1u_pct",
                    "brier",
                    "roi_kelly_vol_pct",
                    "dd_kelly_pct",
                    "sharpe_j",
                ]
            ].to_string(index=False)
        )

    print("\n--- Synthèse (moyenne 3 années, 1 u) ---")
    agg = (
        tbl.groupby("top_n")
        .agg(
            paris_moy=("paris", "mean"),
            hit_moy=("hit_pct", "mean"),
            roi_1u_moy=("roi_1u_pct", "mean"),
            profit_1u_tot=("profit_1u", "sum"),
            brier_moy=("brier", "mean"),
            sharpe_moy=("sharpe_j", "mean"),
        )
        .reset_index()
    )
    agg["paris_moy"] = agg["paris_moy"].round(0).astype(int)
    for c in ("hit_moy", "roi_1u_moy", "brier_moy", "sharpe_moy"):
        agg[c] = agg[c].round(2)
    agg["profit_1u_tot"] = agg["profit_1u_tot"].round(0).astype(int)
    print(agg.to_string(index=False))

    out = os.path.join(ROOT, "data", "reports", "compare_topn_proba_grid_2024_2026.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tbl.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\nExport : {os.path.relpath(out, ROOT)}")


if __name__ == "__main__":
    main()
