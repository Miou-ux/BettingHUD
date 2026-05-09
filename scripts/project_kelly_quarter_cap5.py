"""
Projection bankroll: Kelly 1/4, plafond 5% de la BR courante, pas de stop drawdown.

Lit un CSV produit par backtest_2026.py (colonnes p_model, odd, won, date).
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def simulate(
    df: pd.DataFrame,
    bankroll_start: float,
    kelly_fraction: float,
    max_stake_pct: float,
    *,
    by_day: bool = True,
    return_history: bool = False,
) -> dict:
    """Si by_day=True, tous les paris d'une même date partagent la BR de début de journée
    et la somme des mises est plafonnée à max_stake_pct% de cette BR (répartition
    proportionnelle aux mises Kelly brutes). Évite la recomposition intra-jour irréaliste."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)

    br = float(bankroll_start)
    total_staked = 0.0
    n_bets = 0
    hist = [br]
    history: list[dict] | None = [] if return_history else None

    def _bool_won(v) -> bool:
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        return s in {"1", "true", "yes"}

    if history is not None and len(df):
        d0 = pd.Timestamp(df["date"].iloc[0]).normalize()
        history.append(
            {
                "date": d0 - pd.Timedelta(days=1),
                "bankroll": float(bankroll_start),
                "n_bets_cum": 0,
                "pnl_eur": 0.0,
            }
        )

    if not by_day:
        for _, row in df.iterrows():
            p = float(row["p_model"])
            odd = float(row["odd"])
            won = _bool_won(row["won"])
            b_side = max(0.01, odd - 1.0)
            k_full = max(0.0, (b_side * p - (1.0 - p)) / b_side)
            stake_raw = br * float(kelly_fraction) * k_full
            stake_cap = br * (float(max_stake_pct) / 100.0)
            stake = max(0.0, min(stake_raw, stake_cap))
            if stake <= 0.0:
                continue
            n_bets += 1
            total_staked += stake
            br += stake * (odd - 1.0) if won else -stake
            hist.append(br)
            if history is not None:
                history.append(
                    {
                        "date": pd.Timestamp(row["date"]),
                        "bankroll": float(br),
                        "n_bets_cum": int(n_bets),
                        "pnl_eur": float(br - bankroll_start),
                    }
                )
    else:
        for _, day_df in df.groupby("date", sort=True):
            day_df = day_df.reset_index(drop=True)
            b0 = br
            cap_pool = b0 * (float(max_stake_pct) / 100.0)
            raw_stakes = []
            meta = []
            for _, row in day_df.iterrows():
                p = float(row["p_model"])
                odd = float(row["odd"])
                won = _bool_won(row["won"])
                b_side = max(0.01, odd - 1.0)
                k_full = max(0.0, (b_side * p - (1.0 - p)) / b_side)
                r = b0 * float(kelly_fraction) * k_full
                r = min(r, b0 * (float(max_stake_pct) / 100.0))
                r = max(0.0, r)
                raw_stakes.append(r)
                meta.append((odd, won))
            ssum = sum(raw_stakes)
            if ssum <= 0:
                continue
            scale = 1.0 if ssum <= cap_pool else cap_pool / ssum
            day_pl = 0.0
            for r, (odd, won) in zip(raw_stakes, meta):
                stake = r * scale
                if stake <= 0:
                    continue
                n_bets += 1
                total_staked += stake
                day_pl += stake * (odd - 1.0) if won else -stake
            br = b0 + day_pl
            hist.append(br)
            if history is not None:
                history.append(
                    {
                        "date": pd.Timestamp(day_df["date"].iloc[0]),
                        "bankroll": float(br),
                        "n_bets_cum": int(n_bets),
                        "pnl_eur": float(br - bankroll_start),
                    }
                )

    h = np.array(hist, dtype=float)
    peaks = np.maximum.accumulate(h)
    dd_series = (peaks - h) / np.where(peaks > 0, peaks, 1.0)
    max_dd = float(dd_series.max()) if len(dd_series) else 0.0
    net = br - float(bankroll_start)
    roi_staked = net / total_staked if total_staked > 0 else 0.0

    out: dict = {
        "bankroll_final": br,
        "net_profit_eur": net,
        "growth_pct": (net / bankroll_start) * 100.0 if bankroll_start else 0.0,
        "n_bets": n_bets,
        "total_staked_eur": total_staked,
        "roi_on_staked_pct": roi_staked * 100.0,
        "max_drawdown_pct": max_dd * 100.0,
        "peak_bankroll": float(peaks.max()) if len(peaks) else br,
    }
    if history is not None:
        out["history"] = history
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", help="Chemin vers backtest_YYYY_bets.csv")
    ap.add_argument("--br-start", type=float, default=55.0)
    ap.add_argument("--kelly-frac", type=float, default=0.25, help="Fraction Kelly (0.25 = quart)")
    ap.add_argument("--max-stake-pct", type=float, default=5.0, help="Plafond mise % bankroll")
    ap.add_argument("--sequential", action="store_true", help="Composer la BR après chaque pari (surestime si beaucoup de paris/jour)")
    args = ap.parse_args()

    path = args.csv
    if not os.path.isfile(path):
        print(f"Fichier introuvable: {path}")
        sys.exit(1)

    df = pd.read_csv(path)
    for col in ("p_model", "odd", "won", "date"):
        if col not in df.columns:
            print(f"Colonne manquante: {col}")
            sys.exit(1)

    r = simulate(
        df,
        bankroll_start=args.br_start,
        kelly_fraction=args.kelly_frac,
        max_stake_pct=args.max_stake_pct,
        by_day=not args.sequential,
    )

    print(f"Fichier: {path}")
    print(f"Paris (lignes CSV): {len(df)} | Paris avec mise > 0: {r['n_bets']}")
    print(
        f"BR départ: {args.br_start:.2f} € | BR finale: {r['bankroll_final']:.2f} € "
        f"| P/L: {r['net_profit_eur']:+.2f} € ({r['growth_pct']:+.1f}%)"
    )
    print(f"Volume misé: {r['total_staked_eur']:.2f} € | ROI sur volume: {r['roi_on_staked_pct']:+.2f}%")
    print(f"Max drawdown (sur courbe BR): {r['max_drawdown_pct']:.2f}%")


if __name__ == "__main__":
    main()
