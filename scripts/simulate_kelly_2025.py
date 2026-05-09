"""Simulate Kelly staking variants on 2025 backtest bets."""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd


CSV_PATH = os.path.join("data", "backtest_2025_bets.csv")
START_BANKROLL = 100.0


def kelly_fraction(p_model: np.ndarray, odds: np.ndarray) -> np.ndarray:
    """Full Kelly fraction f* = (bp - q)/b, clipped at 0."""
    b = np.maximum(1e-12, odds - 1.0)
    q = 1.0 - p_model
    f = (b * p_model - q) / b
    return np.maximum(0.0, f)


def simulate(df: pd.DataFrame, frac: float, label: str) -> dict:
    bankroll = float(START_BANKROLL)
    p = df["p_model"].astype(float).values
    odds = df["odd"].astype(float).values
    won = df["won"].astype(bool).values
    k = kelly_fraction(p, odds)

    total_staked = 0.0
    history = [bankroll]
    for i in range(len(df)):
        stake = max(0.0, bankroll * float(frac) * float(k[i]))
        total_staked += stake
        bankroll += stake * (odds[i] - 1.0) if won[i] else -stake
        history.append(bankroll)

    hist = np.asarray(history, dtype=float)
    peaks = np.maximum.accumulate(hist)
    max_dd = float(((peaks - hist) / np.where(peaks > 0, peaks, 1.0)).max())
    net = bankroll - START_BANKROLL
    roi = (net / total_staked) if total_staked > 0 else 0.0
    growth = net / START_BANKROLL
    return {
        "label": label,
        "bankroll_final": bankroll,
        "growth": growth,
        "total_staked": total_staked,
        "roi_on_staked": roi,
        "max_dd": max_dd,
    }


def main() -> None:
    if not os.path.exists(CSV_PATH):
        print(f"Missing {CSV_PATH}. Run the 2025 backtest first.")
        sys.exit(1)

    df = pd.read_csv(CSV_PATH)
    df["date_dt"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date_dt").reset_index(drop=True)

    variants = [
        ("Kelly 1/4", 0.25),
        ("Kelly 1/2", 0.50),
        ("Kelly plein", 1.00),
    ]
    results = [simulate(df, frac=f, label=label) for label, f in variants]

    print(f"=== Simulation Kelly 2025 ({len(df)} paris) ===")
    print(
        f"{'Strategie':14s} {'Bankroll':>10s} {'Croiss.':>9s} "
        f"{'MiseTot':>10s} {'ROI/mise':>10s} {'Max DD':>8s}"
    )
    print("-" * 72)
    for r in results:
        print(
            f"{r['label']:14s} "
            f"{r['bankroll_final']:>9.2f}u "
            f"{r['growth']*100:>+8.1f}% "
            f"{r['total_staked']:>9.2f}u "
            f"{r['roi_on_staked']*100:>+9.2f}% "
            f"{r['max_dd']*100:>7.2f}%"
        )


if __name__ == "__main__":
    main()
