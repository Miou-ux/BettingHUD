"""Kelly 1/4 simulation with bankroll skimming/locking rules on 2025 bets."""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd


CSV_PATH = os.path.join("data", "backtest_2025_bets.csv")

# User scenario
ACTIVE_BR_START = 200.0
KELLY_FRACTION = 0.25
MAX_STAKE_PCT_BR = 5.0
KELLY_CAP = 0.20
EXECUTION_COST_PCT = 2.0

# Profit lock rules
DOUBLE_TARGET = 400.0  # when active BR reaches this level
LOCK_ON_DOUBLE = 200.0  # amount moved to reserve, active BR returns to 200


def kelly_fraction(p_model: np.ndarray, odds: np.ndarray) -> np.ndarray:
    b = np.maximum(1e-12, odds - 1.0)
    q = 1.0 - p_model
    f = (b * p_model - q) / b
    return np.clip(f, 0.0, KELLY_CAP)


def effective_odds(odds: np.ndarray) -> np.ndarray:
    b = np.maximum(0.0, odds - 1.0)
    b_eff = b * (1.0 - EXECUTION_COST_PCT / 100.0)
    return 1.0 + b_eff


def run_simulation(skim_profit_pct: float) -> dict:
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError(CSV_PATH)

    df = pd.read_csv(CSV_PATH)
    df["date_dt"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date_dt").reset_index(drop=True)

    p = df["p_model"].astype(float).values
    odds = effective_odds(df["odd"].astype(float).values)
    won = df["won"].astype(bool).values
    k = kelly_fraction(p, odds)

    br = float(ACTIVE_BR_START)
    reserve = 0.0
    total_staked = 0.0
    doubles = 0
    n_bets = 0

    for i in range(len(df)):
        stake = br * KELLY_FRACTION * float(k[i])
        stake = max(0.0, min(stake, br * (MAX_STAKE_PCT_BR / 100.0)))
        if stake <= 0:
            continue

        n_bets += 1
        total_staked += stake

        pnl = stake * (odds[i] - 1.0) if won[i] else -stake
        br += pnl

        # Optional skim: lock a % of positive pnl into reserve.
        if pnl > 0 and skim_profit_pct > 0:
            skim = pnl * (skim_profit_pct / 100.0)
            skim = max(0.0, min(skim, br))
            br -= skim
            reserve += skim

        # No stop: keep trading forever, but lock at each doubling milestone.
        while br >= DOUBLE_TARGET:
            reserve += LOCK_ON_DOUBLE
            br -= LOCK_ON_DOUBLE
            doubles += 1

    total_equity = br + reserve
    return {
        "skim_profit_pct": skim_profit_pct,
        "active_br_end": br,
        "reserve_end": reserve,
        "total_equity_end": total_equity,
        "profit_vs_start": total_equity - ACTIVE_BR_START,
        "roi_total": (total_equity - ACTIVE_BR_START) / ACTIVE_BR_START,
        "doubles_locked": doubles,
        "total_staked": total_staked,
        "n_bets": n_bets,
    }


def main() -> None:
    # Compare a few practical skimming levels for recommendation.
    levels = [0.0, 5.0, 10.0, 20.0]
    rows = [run_simulation(x) for x in levels]
    out = pd.DataFrame(rows)

    print("=== 2025 Kelly 1/4 with profit-lock (no stop) ===")
    print(
        f"BR active start={ACTIVE_BR_START:.0f}€ | max stake={MAX_STAKE_PCT_BR:.1f}% BR | "
        f"kelly cap={KELLY_CAP:.2f} | friction={EXECUTION_COST_PCT:.1f}%"
    )
    print(
        "Rule: each time active BR >= 400€, move 200€ to reserve and keep trading.\n"
        "Skim% = percent of winning PnL moved to reserve each winning bet.\n"
    )
    print(
        f"{'Skim%':>6s} {'Active fin':>12s} {'Reserve fin':>12s} {'Equity fin':>12s} "
        f"{'ROI total':>10s} {'Doubles':>8s} {'Paris':>7s}"
    )
    print("-" * 82)
    for r in rows:
        print(
            f"{r['skim_profit_pct']:>6.1f} "
            f"{r['active_br_end']:>11.2f}€ "
            f"{r['reserve_end']:>11.2f}€ "
            f"{r['total_equity_end']:>11.2f}€ "
            f"{r['roi_total']*100:>+9.1f}% "
            f"{int(r['doubles_locked']):>8d} "
            f"{int(r['n_bets']):>7d}"
        )


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        print(f"Missing file: {exc}")
        sys.exit(1)
