"""Kelly 1/4 simulation: lock 25% whenever bankroll doubles."""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd


CSV_PATH = os.path.join("data", "backtest_2025_bets.csv")
START_BR = 200.0
KELLY_FRAC = 0.25
MAX_STAKE_PCT_BR = 5.0
KELLY_CAP = 0.20
EXEC_COST_PCT = 2.0
LOCK_PCT_ON_DOUBLE = 25.0


def kelly_fraction(p_model: np.ndarray, odds: np.ndarray) -> np.ndarray:
    b = np.maximum(1e-12, odds - 1.0)
    q = 1.0 - p_model
    f = (b * p_model - q) / b
    return np.clip(f, 0.0, KELLY_CAP)


def effective_odds(odds: np.ndarray) -> np.ndarray:
    b = np.maximum(0.0, odds - 1.0)
    b_eff = b * (1.0 - EXEC_COST_PCT / 100.0)
    return 1.0 + b_eff


def main() -> None:
    if not os.path.exists(CSV_PATH):
        print(f"Missing {CSV_PATH}")
        sys.exit(1)

    df = pd.read_csv(CSV_PATH)
    df["date_dt"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date_dt").reset_index(drop=True)

    p = df["p_model"].astype(float).values
    odds = effective_odds(df["odd"].astype(float).values)
    won = df["won"].astype(bool).values
    k = kelly_fraction(p, odds)

    br = float(START_BR)
    reserve = 0.0
    total_staked = 0.0
    n_bets = 0

    # Double trigger logic: each time BR reaches 2x current anchor.
    anchor = float(START_BR)
    next_double = 2.0 * anchor
    lock_events = 0

    for i in range(len(df)):
        stake = br * KELLY_FRAC * float(k[i])
        stake = max(0.0, min(stake, br * (MAX_STAKE_PCT_BR / 100.0)))
        if stake <= 0:
            continue

        n_bets += 1
        total_staked += stake
        br += stake * (odds[i] - 1.0) if won[i] else -stake

        # No stop: just lock reserve on doubling events.
        while br >= next_double:
            lock_amount = br * (LOCK_PCT_ON_DOUBLE / 100.0)
            reserve += lock_amount
            br -= lock_amount
            lock_events += 1
            anchor = br
            next_double = 2.0 * anchor

    total_equity = br + reserve
    roi = (total_equity - START_BR) / START_BR
    roi_staked = (total_equity - START_BR) / total_staked if total_staked > 0 else 0.0

    print("=== 2025 Kelly 1/4 - lock 25% on each doubling ===")
    print(
        f"Start={START_BR:.0f}€ | max stake={MAX_STAKE_PCT_BR:.1f}% BR | "
        f"kelly cap={KELLY_CAP:.2f} | friction={EXEC_COST_PCT:.1f}% | no stop"
    )
    print(f"Paris: {n_bets}")
    print(f"Lock events: {lock_events}")
    print(f"Bankroll active finale: {br:.2f}€")
    print(f"Reserve finale: {reserve:.2f}€")
    print(f"Equity totale finale: {total_equity:.2f}€")
    print(f"ROI total: {roi*100:+.2f}%")
    print(f"ROI sur mises totales: {roi_staked*100:+.2f}%")


if __name__ == "__main__":
    main()
