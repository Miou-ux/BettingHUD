"""Exporte les N premiers paris de la simulation top-10 / EV 15 % avec mises Kelly."""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import kelly_full_fraction, load_and_filter_bets_csv
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key
from scripts.simulate_top10_proba_2026 import KELLY_BASE, MAX_STAKE_PCT, select_top_proba_per_day


def export_bet_ledger(
    csv_path: str,
    *,
    year: int = 2026,
    top_n: int = 10,
    ev_min_pct: float = 15.0,
    br0: float = 100.0,
    limit: int = 100,
    out_path: str | None = None,
) -> pd.DataFrame:
    df_f = load_and_filter_bets_csv(
        csv_path,
        year=year,
        ev_min_pct=ev_min_pct,
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=["G", "M", "A"],
        extra_tournament_tokens=[
            "olympics",
            "davis cup",
            "billie jean king cup",
            "united cup",
            "atp finals",
            "wta finals",
            "laver cup",
        ],
    )
    df = select_top_proba_per_day(df_f, top_n=top_n)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["_ord"] = np.arange(len(df), dtype=np.int64)
    df = (
        df.sort_values(["date", "p_model", "_ord"], ascending=[True, False, True], kind="mergesort")
        .drop(columns=["_ord"])
        .reset_index(drop=True)
    )

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))

    def _seg_key(row: pd.Series) -> str:
        return resolve_match_brier_segment_key(
            ml,
            tour=row.get("tour"),
            surface=row.get("surface"),
            tournament=row.get("tournament"),
            tourney_level=row.get("tourney_level"),
        )

    adapt_frac = KELLY_BASE
    cap_frac = MAX_STAKE_PCT / 100.0
    br = float(br0)
    records: list[dict] = []

    for day, day_df in df.groupby("date", sort=True):
        day_df = day_df.reset_index(drop=True)
        b0 = br
        liquid = b0
        day_deploy_cap = b0
        day_deploy_used = 0.0
        day_rank = 0
        day_iso = pd.Timestamp(day).strftime("%Y-%m-%d")
        for _, row in day_df.iterrows():
            day_rank += 1
            odd = float(row["odd"])
            p = float(row["p_model"])
            won = str(row["won"]).strip().lower() in {"1", "true", "yes"}
            kf = kelly_full_fraction(p, odd)
            sk = _seg_key(row)
            brier_seg = float(seg.get(sk, glob_b))
            kelly_adj = max(0.0, 1.0 - (brier_seg / 0.25))
            stake_frac = max(0.0, (adapt_frac * kf) * kelly_adj)
            raw = liquid * stake_frac
            cap_lim = max(0.0, liquid) * cap_frac
            remaining = max(0.0, day_deploy_cap - day_deploy_used)
            stake = max(0.0, min(raw, cap_lim, liquid, remaining))
            cap_hit = raw > cap_lim + 1e-9
            pnl = stake * (odd - 1.0) if won and stake > 0 else (-stake if stake > 0 else 0.0)
            side = str(row.get("side", "")).upper()
            if side == "WINNER":
                bet_player = row.get("winner_name", "")
                opp = row.get("loser_name", "")
            else:
                bet_player = row.get("loser_name", "")
                opp = row.get("winner_name", "")
            records.append(
                {
                    "n": len(records) + 1,
                    "date": day_iso,
                    "rang_jour": day_rank,
                    "tour": row.get("tour"),
                    "tournament": row.get("tournament"),
                    "surface": row.get("surface"),
                    "niveau": row.get("tourney_level"),
                    "cote_source": row.get("odds_source"),
                    "joueur_pari": bet_player,
                    "adversaire": opp,
                    "cote": round(odd, 3),
                    "p_model_pct": round(p * 100, 2),
                    "p_implicite_pct": round(float(row.get("p_implied", 0)) * 100, 2),
                    "ev_pct": round(float(row.get("ev", 0)) * 100, 2),
                    "segment_key": sk,
                    "segment_brier": round(brier_seg, 4),
                    "kelly_full_pct": round(kf * 100, 2),
                    "stake_frac_pct": round(stake_frac * 100, 2),
                    "mise_eur": round(stake, 2),
                    "cap_15pct": "oui" if cap_hit else "non",
                    "liquidite_avant_eur": round(liquid, 2),
                    "br_matin_eur": round(b0, 2),
                    "resultat": "G" if won else "P",
                    "pnl_eur": round(pnl, 2),
                }
            )
            if stake > 0:
                liquid -= stake
                day_deploy_used += stake
        br = b0 + sum(r["pnl_eur"] for r in records if r["date"] == day_iso)
        for r in records:
            if r["date"] == day_iso:
                r["br_apres_jour_eur"] = round(br, 2)

    out = pd.DataFrame(records[: max(0, int(limit))])
    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        out.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "data", "backtest_2026_bets.csv"))
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument(
        "--out",
        default=os.path.join(ROOT, "data", "reports", "backtest_top10_2026_first100_bets.csv"),
    )
    args = ap.parse_args()
    out = export_bet_ledger(args.csv, limit=args.limit, out_path=args.out)
    print(f"Export : {args.out} ({len(out)} lignes)")
    cols = [
        "n",
        "date",
        "rang_jour",
        "tour",
        "joueur_pari",
        "cote",
        "p_model_pct",
        "ev_pct",
        "mise_eur",
        "resultat",
        "pnl_eur",
        "br_apres_jour_eur",
    ]
    pd.set_option("display.max_colwidth", 22)
    pd.set_option("display.width", 220)
    print(out[cols].to_string(index=False))


if __name__ == "__main__":
    main()
