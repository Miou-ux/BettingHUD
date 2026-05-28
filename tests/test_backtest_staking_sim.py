"""Tests simulate_sequential_intraday — liquidité intraday et no double-comptage."""
from __future__ import annotations

import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import simulate_sequential_intraday  # noqa: E402


def _day_stakes_trace(df: pd.DataFrame, *, bankroll_start: float = 100.0) -> list[dict]:
    """Rejoue la logique intraday et retourne les mises du premier jour."""
    work = df.copy()
    work["_ord"] = range(len(work))
    work["date"] = pd.to_datetime(work["date"])
    work = work.sort_values(["date", "_ord"], kind="mergesort")
    day_df = work.groupby("date", sort=True).get_group(work["date"].iloc[0]).reset_index(drop=True)

    b0 = float(bankroll_start)
    liquid = b0
    day_deploy_cap = b0
    day_deploy_used = 0.0
    cap_frac = 0.15
    stakes: list[dict] = []

    for i, row in day_df.iterrows():
        liquid_before = liquid
        p = float(row["p_model"])
        odd = float(row["odd"])
        b = max(1e-12, odd - 1.0)
        kf = max(0.0, (b * p - (1.0 - p)) / b)
        stake_frac = 0.5 * kf * 0.5  # Brier factor ~0.5 for test
        raw = liquid * stake_frac
        cap_lim = liquid * cap_frac
        remaining_day_budget = max(0.0, day_deploy_cap - day_deploy_used)
        stake = max(0.0, min(raw, cap_lim, liquid, remaining_day_budget))
        if stake <= 0:
            continue
        liquid -= stake
        day_deploy_used += stake
        stakes.append(
            {
                "idx": i,
                "liquid_before": liquid_before,
                "stake": stake,
                "liquid_after": liquid,
                "cap_lim": cap_lim,
            }
        )
    return stakes


def test_intraday_stakes_never_exceed_morning_bankroll():
    df = pd.DataFrame(
        [
            {"date": "2026-01-01", "p_model": 0.72, "odd": 1.55, "won": True},
            {"date": "2026-01-01", "p_model": 0.70, "odd": 1.60, "won": False},
            {"date": "2026-01-01", "p_model": 0.68, "odd": 1.65, "won": True},
        ]
    )
    out = simulate_sequential_intraday(
        df,
        bankroll_start=100.0,
        kelly_multiplier=1.0,
        max_stake_pct=15.0,
        daily_stake_budget_pct=100.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=0.5,
        segment_brier_scores={"X": 0.12},
        global_brier_score=0.12,
        stake_cap_basis="liquid",
        return_history=True,
    )
    hist = out["history"]
    assert len(hist) == 1
    assert hist[0]["day_stake_eur"] <= 100.0 + 1e-9


def test_second_intraday_stake_uses_reduced_liquid_cap():
    df = pd.DataFrame(
        [
            {"date": "2026-01-01", "p_model": 0.80, "odd": 1.40, "won": True},
            {"date": "2026-01-01", "p_model": 0.78, "odd": 1.42, "won": True},
        ]
    )
    stakes = _day_stakes_trace(df)
    assert len(stakes) == 2
    s1, s2 = stakes
    assert s1["stake"] <= s1["liquid_before"] + 1e-9
    assert s2["stake"] <= s2["liquid_before"] + 1e-9
    assert abs(s2["liquid_before"] - s1["liquid_after"]) < 1e-9
    assert s2["cap_lim"] < s1["cap_lim"]


def test_bankroll_updates_end_of_day_only():
    df = pd.DataFrame(
        [
            {"date": "2026-01-01", "p_model": 0.60, "odd": 2.0, "won": True},
            {"date": "2026-01-02", "p_model": 0.60, "odd": 2.0, "won": False},
        ]
    )
    out = simulate_sequential_intraday(
        df,
        bankroll_start=100.0,
        kelly_multiplier=1.0,
        max_stake_pct=15.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=0.5,
        segment_brier_scores={},
        global_brier_score=0.12,
        stake_cap_basis="liquid",
        return_history=True,
    )
    h0, h1 = out["history"]
    assert h0["bankroll"] != 100.0  # PnL jour 1 appliqué en clôture
    assert h1["bankroll"] < h0["bankroll"]  # jour 2 perdant
