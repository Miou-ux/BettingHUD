"""Confidence-based staking vs flat 1u on 2025 backtest bets.

Confidence (same as ml_model.predict_match): ``abs(p_model - 0.5) * 2`` where
``p_model`` is the model probability on the **bet side** (already stored in
``backtest_*_bets.csv``).

Policies implemented:
- ``tier_prod``: same buckets as ``simulate_day.py`` (_stake_multiplier).
- ``tier_smooth``: wider gaps to avoid over-concentration on few bets.
- ``linear``: stake proportional to confidence (scaled so mean stake ~= 1u).
- ``sqrt``: stake ~ sqrt(confidence) for less aggressive scaling.

Usage::

    python -m scripts.compare_confidence_staking_2025
"""
from __future__ import annotations

import os
import sys
import io

import numpy as np
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CSV_PATH = os.path.join("data", "backtest_2025_bets.csv")
START_BANKROLL = 100.0


def confidence_from_p_model(p: pd.Series) -> pd.Series:
    return ((p.astype(float) - 0.5).abs() * 2.0).clip(0.0, 1.0)


def mult_tier_prod(c: float) -> float:
    """Identical to simulate_day._stake_multiplier on confidence."""
    if c < 0.25:
        return 0.50
    if c < 0.50:
        return 0.75
    if c < 0.75:
        return 1.00
    return 1.25


def mult_tier_smooth(c: float) -> float:
    """Softer ramp — caps extremes."""
    if c < 0.30:
        return 0.60
    if c < 0.55:
        return 0.85
    if c < 0.78:
        return 1.05
    return 1.20


def simulate(
    df: pd.DataFrame,
    *,
    weights: np.ndarray,
    label: str,
    start: float = START_BANKROLL,
) -> dict:
    """weights[i] = multiplier × base unit (same bet sequence as flat 1u)."""
    rets = df["ret"].astype(float).values * weights
    stakes = weights.astype(float)
    bankroll = float(start)
    peak = bankroll
    hist = [bankroll]
    for r in rets:
        bankroll += r
        peak = max(peak, bankroll)
        hist.append(bankroll)
    hist = np.asarray(hist)
    peaks = np.maximum.accumulate(hist)
    dd = float(((peaks - hist) / np.where(peaks > 0, peaks, 1.0)).max())
    total_staked = float(stakes.sum())
    net = float(rets.sum())
    roi = net / total_staked if total_staked > 0 else 0.0
    n = len(df)
    pnl = rets.astype(float)
    std_p = float(pnl.std(ddof=1)) if n > 1 else 0.0
    sharpe = float((pnl.mean() / std_p) * np.sqrt(n)) if std_p > 0 else 0.0
    return {
        "label": label,
        "bankroll_final": float(bankroll),
        "growth": (bankroll - start) / start,
        "roi_on_staked": roi,
        "total_staked": total_staked,
        "net_profit": net,
        "max_dd": dd,
        "sharpe": sharpe,
        "avg_stake": total_staked / n,
        "min_w": float(weights.min()),
        "max_w": float(weights.max()),
    }


def main() -> None:
    if not os.path.exists(CSV_PATH):
        print(f"Missing {CSV_PATH} — run backtest first.")
        sys.exit(1)
    df = pd.read_csv(CSV_PATH)
    df["date_dt"] = pd.to_datetime(df["date"])
    df = df.sort_values("date_dt").reset_index(drop=True)
    conf = confidence_from_p_model(df["p_model"])

    n = len(df)
    flat = np.ones(n)

    w_tier = np.array([mult_tier_smooth(float(c)) for c in conf])
    w_tier_p = np.array([mult_tier_prod(float(c)) for c in conf])

    # Linear: raw conf as weight then rescale so mean weight = 1 (fair vs flat capital intensity)
    w_lin_raw = conf.values.astype(float)
    w_lin = w_lin_raw / (w_lin_raw.mean() + 1e-12)

    w_sqrt = np.sqrt(conf.values.astype(float))
    w_sqrt = w_sqrt / (w_sqrt.mean() + 1e-12)

    edge_pp = (df["p_model"].astype(float) - df["p_implied"].astype(float)).abs() * 100.0
    w_edge = edge_pp.values.astype(float)
    w_edge = w_edge / (w_edge.mean() + 1e-12)

    results = [
        simulate(df, weights=flat, label="Mise plate 1 u"),
        simulate(df, weights=w_tier_p, label="Confiance — paliers (dashboard / simulate_day)"),
        simulate(df, weights=w_tier, label="Confiance — paliers adoucis"),
        simulate(df, weights=w_lin, label="Confiance — linéaire (mean stake = 1u)"),
        simulate(df, weights=w_sqrt, label="Confiance — √ (mean stake = 1u)"),
        simulate(df, weights=w_edge, label="Edge — linéaire |p−implicite| (mean stake = 1u)"),
    ]

    print(f"=== Backtest 2025 — {n} paris — mise selon la confiance ===\n")
    print(
        "Confiance = |p_modèle(côté parié) − 0.5| × 2   "
        "(0 = coin-flip, 1 = très net)\n"
    )
    print(
        f"{'Stratégie':48s} {'Bankroll':>10s} {'Croiss.':>9s} {'ROI/mise':>10s} {'Max DD':>8s} {'Sharpe':>8s} {'mise moy':>9s} {'w min/max':>12s}"
    )
    print("=" * 120)
    for r in results:
        print(
            f"{r['label']:48s} "
            f"{r['bankroll_final']:>9.2f}u "
            f"{r['growth']*100:>+8.1f}% "
            f"{r['roi_on_staked']*100:>+9.2f}% "
            f"{r['max_dd']*100:>7.2f}% "
            f"{r['sharpe']:>+8.2f} "
            f"{r['avg_stake']:>8.3f}u "
            f"{r['min_w']:.2f}/{r['max_w']:.2f}"
        )

    # Distribution of confidence
    print("\n=== Répartition de la confiance sur les paris ===")
    print(conf.describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).to_string())

    # Per tour: best policy (tier_smooth)
    print("\n=== ATP / WTA — paliers adoucis vs plate ===")
    for tour in ("ATP", "WTA"):
        sub = df[df["tour"] == tour].reset_index(drop=True)
        if sub.empty:
            continue
        csub = confidence_from_p_model(sub["p_model"])
        w = np.array([mult_tier_smooth(float(x)) for x in csub])
        rf = simulate(sub, weights=np.ones(len(sub)), label="flat")
        rt = simulate(sub, weights=w, label="tier")
        print(
            f"  {tour}: flat → bankroll {rf['bankroll_final']:.1f}u ({rf['growth']*100:+.1f}%) | "
            f"tier → {rt['bankroll_final']:.1f}u ({rt['growth']*100:+.1f}%) | "
            f"ROI/mise flat {rf['roi_on_staked']*100:+.2f}% tier {rt['roi_on_staked']*100:+.2f}%"
        )

    out = os.path.join("data", "backtest_2025_confidence_staking.csv")
    pd.DataFrame(results).to_csv(out, index=False)
    print(f"\n[OK] {out}")


if __name__ == "__main__":
    main()
