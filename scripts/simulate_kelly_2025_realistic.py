"""Realistic Kelly simulation on 2025 backtest bets."""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd


CSV_PATH = os.path.join("data", "backtest_2025_bets.csv")

# Realistic profile
START_BANKROLL = 200.0  # euros
MAX_STAKE_PCT_BANKROLL = 5.0  # hard cap per bet
KELLY_CAP = 0.20  # cap raw Kelly fraction
EXECUTION_COST_PCT = 2.0  # slippage/commission proxy on payout leg
STOP_DRAWDOWN_PCT = 30.0  # stop strategy if drawdown exceeds this level


def kelly_fraction(p_model: np.ndarray, odds: np.ndarray) -> np.ndarray:
    """Full Kelly fraction f* = (bp - q)/b, clipped in [0, KELLY_CAP]."""
    b = np.maximum(1e-12, odds - 1.0)
    q = 1.0 - p_model
    f = (b * p_model - q) / b
    return np.clip(f, 0.0, KELLY_CAP)


def apply_execution_cost(odds: np.ndarray) -> np.ndarray:
    """Reduce effective odds on winning bets only."""
    b = np.maximum(0.0, odds - 1.0)
    b_eff = b * (1.0 - EXECUTION_COST_PCT / 100.0)
    return 1.0 + b_eff


def simulate(df: pd.DataFrame, frac: float, label: str) -> dict:
    bankroll = float(START_BANKROLL)
    p = df["p_model"].astype(float).values
    odds = df["odd"].astype(float).values
    odds_eff = apply_execution_cost(odds)
    won = df["won"].astype(bool).values
    k = kelly_fraction(p, odds)

    total_staked = 0.0
    history = [bankroll]
    n_bets = 0
    peak_bankroll = bankroll
    stop_hit = False
    stop_index = -1
    for i in range(len(df)):
        if stop_hit:
            history.append(bankroll)
            continue

        if peak_bankroll > 0:
            dd_now = (peak_bankroll - bankroll) / peak_bankroll * 100.0
            if dd_now >= STOP_DRAWDOWN_PCT:
                stop_hit = True
                stop_index = i
                history.append(bankroll)
                continue

        stake_raw = bankroll * float(frac) * float(k[i])
        stake_cap = bankroll * (MAX_STAKE_PCT_BANKROLL / 100.0)
        stake = max(0.0, min(stake_raw, stake_cap))
        if stake <= 0.0:
            history.append(bankroll)
            continue
        n_bets += 1
        total_staked += stake
        bankroll += stake * (odds_eff[i] - 1.0) if won[i] else -stake
        peak_bankroll = max(peak_bankroll, bankroll)
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
        "n_bets": n_bets,
        "stop_hit": stop_hit,
        "stop_index": stop_index,
        "n_skipped_after_stop": max(0, len(df) - stop_index) if stop_hit else 0,
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

    print(f"=== Simulation Kelly REALISTE 2025 ({len(df)} paris) ===")
    print(
        f"Capital initial={START_BANKROLL:.0f} EUR | cap mise={MAX_STAKE_PCT_BANKROLL:.1f}% bankroll "
        f"| cap Kelly={KELLY_CAP:.2f} | friction={EXECUTION_COST_PCT:.1f}% "
        f"| stop DD={STOP_DRAWDOWN_PCT:.0f}%"
    )
    print(
        f"{'Strategie':14s} {'Bankroll':>11s} {'Croiss.':>9s} {'MiseTot':>11s} "
        f"{'ROI/mise':>10s} {'Max DD':>8s} {'Paris':>7s} {'Stop':>6s}"
    )
    print("-" * 94)
    for r in results:
        print(
            f"{r['label']:14s} "
            f"{r['bankroll_final']:>10.2f}€ "
            f"{r['growth']*100:>+8.1f}% "
            f"{r['total_staked']:>10.2f}€ "
            f"{r['roi_on_staked']*100:>+9.2f}% "
            f"{r['max_dd']*100:>7.2f}% "
            f"{r['n_bets']:>7d} "
            f"{('oui' if r['stop_hit'] else 'non'):>6s}"
        )


if __name__ == "__main__":
    main()
