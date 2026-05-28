#!/usr/bin/env python3
"""Export + métriques performance sur une fenêtre de dates (match_date / detected_date)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.bets_db import read_algo_opportunity_report  # noqa: E402


def _max_drawdown_pct(cum: pd.Series) -> float:
    if cum.empty:
        return 0.0
    peak = cum.cummax()
    dd = (cum - peak) / peak.replace(0, pd.NA) * 100.0
    return float(dd.min()) if dd.notna().any() else 0.0


def _bet_metrics(df: pd.DataFrame, label: str) -> dict:
    if df.empty:
        return {"label": label, "n": 0}
    closed = df[df["status"].isin(["Gagné", "Perdu"])].copy()
    cancelled = df[df["status"] == "Annulé"]
    open_ = df[df["status"] == "En cours"]
    profit = float(df.loc[df["status"] != "En cours", "profit"].sum())
    staked_closed = float(closed["stake"].sum()) if not closed.empty else 0.0
    staked_all = float(df["stake"].sum())
    wins = int((closed["status"] == "Gagné").sum()) if not closed.empty else 0
    losses = int((closed["status"] == "Perdu").sum()) if not closed.empty else 0
    n_closed = len(closed)
    cum = df.sort_values(["match_date_eff", "id"])["profit"].cumsum()
    out = {
        "label": label,
        "n_total": int(len(df)),
        "n_closed": n_closed,
        "n_open": int(len(open_)),
        "n_cancelled": int(len(cancelled)),
        "wins": wins,
        "losses": losses,
        "hit_rate_pct": wins / max(1, n_closed) * 100.0,
        "profit_u": profit,
        "stake_closed_u": staked_closed,
        "stake_total_u": staked_all,
        "roi_closed_pct": (profit / staked_closed * 100.0) if staked_closed > 0 else 0.0,
        "yield_pct": (profit / staked_all * 100.0) if staked_all > 0 else 0.0,
        "avg_odd": float(closed["odds"].mean()) if not closed.empty else None,
        "avg_stake": float(closed["stake"].mean()) if not closed.empty else None,
        "avg_profit_per_bet": (profit / n_closed) if n_closed else 0.0,
        "max_drawdown_pct": _max_drawdown_pct(cum),
    }
    for col in ("ev_at_bet", "p_model", "p_implicit", "clv_score"):
        if col in df.columns and df[col].notna().any():
            s = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(s):
                out[f"avg_{col}"] = float(s.mean())
                if col == "ev_at_bet":
                    out["avg_ev_pct"] = float(s.mean() * 100.0)
                if col == "p_model":
                    out["avg_p_model_pct"] = float(s.mean() * 100.0)
    if "clv_score" in df.columns and closed["clv_score"].notna().any():
        out["avg_clv_closed"] = float(
            pd.to_numeric(closed["clv_score"], errors="coerce").dropna().mean()
        )
        out["pct_positive_clv"] = float(
            (pd.to_numeric(closed["clv_score"], errors="coerce") > 0).mean() * 100.0
        )
    return out


def _group_table(df: pd.DataFrame, col: str) -> list[dict]:
    if col not in df.columns or df.empty:
        return []
    rows = []
    for key, g in df.groupby(col, dropna=False):
        m = _bet_metrics(g, str(key))
        m["group"] = str(key) if key is not None and str(key) != "nan" else "—"
        rows.append(m)
    return sorted(rows, key=lambda r: r.get("profit_u", 0), reverse=True)


def _ev_bucket_performance(df: pd.DataFrame) -> list[dict]:
    if df.empty or "ev_at_bet" not in df.columns:
        return []
    closed = df[df["status"].isin(["Gagné", "Perdu"])].copy()
    if closed.empty:
        return []
    ev = pd.to_numeric(closed["ev_at_bet"], errors="coerce") * 100.0
    closed = closed.assign(ev_pct=ev)
    bins = [0, 5, 10, 15, 100]
    labels = ["0-5%", "5-10%", "10-15%", "15%+"]
    closed["ev_bucket"] = pd.cut(
        closed["ev_pct"], bins=bins, labels=labels, right=False
    )
    out = []
    for b, g in closed.groupby("ev_bucket", observed=True):
        if pd.isna(b):
            continue
        wins = int((g["status"] == "Gagné").sum())
        out.append(
            {
                "bucket": str(b),
                "n": int(len(g)),
                "hit_rate_pct": wins / max(1, len(g)) * 100.0,
                "profit_u": float(g["profit"].sum()),
                "roi_pct": float(g["profit"].sum() / g["stake"].sum() * 100.0)
                if g["stake"].sum() > 0
                else 0.0,
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-05-18")
    parser.add_argument("--end", default="2026-05-20")
    parser.add_argument(
        "--out-dir",
        default=os.path.join("data", "exports"),
    )
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    tag = f"{args.start.replace('-', '')}_{args.end.replace('-', '')}"

    conn = sqlite3.connect("data/bettinghud.db")
    bets = pd.read_sql(
        """
        SELECT *,
               COALESCE(NULLIF(trim(match_date), ''), date) AS match_date_eff
        FROM user_bets
        WHERE COALESCE(NULLIF(trim(match_date), ''), date) >= ?
          AND COALESCE(NULLIF(trim(match_date), ''), date) <= ?
        ORDER BY match_date_eff, id
        """,
        conn,
        params=(args.start, args.end),
    )
    conn.close()

    bets_path = os.path.join(args.out_dir, f"portfolio_bets_{tag}.csv")
    bets.to_csv(bets_path, index=False, encoding="utf-8-sig")

    algo = read_algo_opportunity_report(start_date=args.start, end_date=args.end)
    algo_rows = algo.get("rows") or []
    algo_df = pd.DataFrame(algo_rows) if algo_rows else pd.DataFrame()
    algo_path = os.path.join(args.out_dir, f"algo_opportunities_{tag}.csv")
    if not algo_df.empty:
        algo_df.to_csv(algo_path, index=False, encoding="utf-8-sig")

    # Real bets linked to algo
    linked = []
    if not algo_df.empty and not bets.empty:
        linked_ids = set(algo_df["linked_bet_id"].dropna().astype(int))
        linked_bets = bets[bets["id"].isin(linked_ids)]
    else:
        linked_bets = bets.iloc[0:0]

    daily = []
    if not bets.empty:
        for d, g in bets.groupby("match_date_eff"):
            m = _bet_metrics(g, str(d))
            m["date"] = str(d)
            daily.append(m)

    report = {
        "window": {"start": args.start, "end": args.end, "generated_at": datetime.now().isoformat()},
        "files": {"bets_csv": bets_path, "algo_csv": algo_path if not algo_df.empty else None},
        "portfolio_all": _bet_metrics(bets, "Tous paris réels"),
        "portfolio_live_tracker": _bet_metrics(
            bets[bets.get("tracker_source", pd.Series(dtype=str)).fillna("") == "live_tracker"],
            "Live Tracker",
        ),
        "portfolio_legacy": _bet_metrics(
            bets[bets.get("tracker_source", pd.Series(dtype=str)).fillna("") != "live_tracker"],
            "Hors live_tracker",
        ),
        "portfolio_linked_to_algo": _bet_metrics(linked_bets, "Paris liés au report algo"),
        "by_day": daily,
        "by_tour": _group_table(bets, "tour"),
        "by_surface": _group_table(bets, "surface"),
        "by_status": _group_table(bets, "status"),
        "ev_buckets": _ev_bucket_performance(bets),
        "algo_theoretical": algo.get("metrics") or {},
        "algo_n_rows": len(algo_rows),
    }

    # Algo by day from rows
    if algo_rows:
        adf = pd.DataFrame(algo_rows)
        algo_daily = []
        for d, g in adf.groupby("detected_date"):
            resolved = g[g["status"].isin(["Gagné", "Perdu", "Annulé"])]
            won = resolved[resolved["status"] == "Gagné"]
            non_cancel = resolved[resolved["status"] != "Annulé"]
            theo_p = float(
                non_cancel["theoretical_profit_eur"].sum()
                if "theoretical_profit_eur" in non_cancel.columns
                else non_cancel.get("theoretical_profit", pd.Series(dtype=float)).sum()
            )
            theo_s = float(
                non_cancel["theoretical_stake_used_eur"].sum()
                if "theoretical_stake_used_eur" in non_cancel.columns
                else 0.0
            )
            algo_daily.append(
                {
                    "date": str(d),
                    "n_detected": int(len(g)),
                    "n_resolved": int(len(resolved)),
                    "hit_rate_pct": len(won) / max(1, len(non_cancel)) * 100.0,
                    "theo_profit_u": theo_p,
                    "theo_roi_pct": (theo_p / theo_s * 100.0) if theo_s > 0 else 0.0,
                    "n_real_bets": int(g["linked_bet_id"].notna().sum()),
                }
            )
        report["algo_by_day"] = algo_daily

    json_path = os.path.join(args.out_dir, f"performance_report_{tag}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print(f"\nExport: {bets_path}")
    if not algo_df.empty:
        print(f"Export: {algo_path}")
    print(f"Rapport: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
