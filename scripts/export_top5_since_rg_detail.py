#!/usr/bin/env python3
"""Export détail Top 5 proba/jour depuis RG (algo_opportunities, même logique replay)."""
from __future__ import annotations

import os
import sqlite3
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import (
    sync_algo_opportunities_from_results,
    sync_daily_top_proba_from_results,
)
from scripts.replay_live_top5_proba import (
    apply_ev_band,
    dedupe_opportunities,
    load_algo_opportunities,
    select_top_proba_per_day,
)

STAKE = 5.0


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-05-18", help="match_date min (YYYY-MM-DD)")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    ap.add_argument("--top-n", type=int, default=5, help="Max paris/jour (apres filtre proba)")
    ap.add_argument(
        "--p-min-pct",
        type=float,
        default=0.0,
        help="Proba modele min %% (ex. 65 = uniquement p_model >= 65%%)",
    )
    ap.add_argument(
        "--out",
        default="",
        help="CSV sortie (defaut: data/reports/top5_since_{start}_detail.csv)",
    )
    args = ap.parse_args()
    start = str(args.start).strip()
    db_path = str(args.db)
    top_n = max(0, int(args.top_n))
    p_min_pct = max(0.0, float(args.p_min_pct))
    p_min_frac = p_min_pct / 100.0 if p_min_pct > 0 else 0.0
    suffix = f"_p{int(p_min_pct)}" if p_min_pct > 0 else ""
    out = args.out.strip() or os.path.join(
        ROOT,
        "data",
        "reports",
        f"top5_since_{start.replace('-', '')}{suffix}_detail.csv",
    )

    cn = sqlite3.connect(db_path)
    sync_algo_opportunities_from_results(cn)
    sync_daily_top_proba_from_results(cn)
    cn.close()

    raw = load_algo_opportunities(db_path=db_path, start=start)
    pool = apply_ev_band(dedupe_opportunities(raw))
    if p_min_frac > 0:
        pool = pool[pool["p_model"].astype(float) >= p_min_frac].copy()
    sel = select_top_proba_per_day(pool, top_n=top_n) if top_n > 0 else pool

    rows = []
    for _, r in sel.iterrows():
        settled = bool(r.get("settled"))
        won = bool(r.get("won")) if settled else None
        odd = float(r["odd"])
        pnl = (odd - 1.0) * STAKE if settled and won else (-STAKE if settled else None)
        rows.append(
            {
                "match_date": str(r.get("match_date") or "")[:10],
                "tour": r.get("tour"),
                "tournament": r.get("tournament"),
                "bet_on": r.get("bet_on"),
                "match_name": r.get("match_name"),
                "p_model_pct": round(float(r["p_model"]) * 100, 1),
                "ev_pct": round(float(r["ev_pct"]), 1),
                "odd": round(odd, 2),
                "status": r.get("st"),
                "settled": settled,
                "won": won,
                "pnl_5eur": round(pnl, 2) if pnl is not None else None,
            }
        )

    df = pd.DataFrame(rows)
    df = df.sort_values(["match_date", "p_model_pct"], ascending=[True, False])
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")

    settled = df[df["settled"]]
    open_n = df[~df["settled"]]
    profit = float(settled["pnl_5eur"].sum()) if not settled.empty else 0.0
    staked = float(len(settled) * STAKE)
    hit = float(settled["won"].mean() * 100) if not settled.empty else 0.0
    wins = int(settled["won"].sum()) if not settled.empty else 0

    filt = f" · proba >= {p_min_pct:.0f}%" if p_min_pct > 0 else ""
    print(
        f"Periode depuis {start} · Top {top_n} proba/j · EV 15-100%{filt} · replay live"
    )
    print(f"DB: {db_path}")
    print(f"Selection: {len(df)} paris · Regles: {len(settled)} · En cours: {len(open_n)}")
    print(f"Hit: {hit:.1f}% ({wins}G / {len(settled) - wins}P)")
    print(f"Mises: {staked:.0f} EUR · Profit net (5 EUR): {profit:+.2f} EUR")
    if staked > 0:
        print(f"ROI sur mises: {profit / staked * 100:+.1f}%")
    print(f"BR 100 EUR -> {100 + profit:.2f} EUR")
    print(f"Export: {out}\n")

    for _, r in settled.iterrows():
        w = "G" if r["won"] else "P"
        print(
            f"{r['match_date']} | {w} | proba {r['p_model_pct']:5.1f}% | "
            f"EV {r['ev_pct']:+5.1f}% | @{r['odd']:.2f} | {r['pnl_5eur']:+6.2f} EUR | "
            f"{str(r['bet_on'])[:32]:32} | {str(r['tournament'])[:24]}"
        )

    if not open_n.empty:
        print("\n--- En cours (hors PnL) ---")
        for _, r in open_n.iterrows():
            print(
                f"{r['match_date']} | ... | proba {r['p_model_pct']:5.1f}% | "
                f"EV {r['ev_pct']:+5.1f}% | @{r['odd']:.2f} | "
                f"{str(r['bet_on'])[:32]:32} | {str(r['tournament'])[:24]}"
            )


if __name__ == "__main__":
    main()
