"""Model drift / calibration analytics on the user's real bets.

Compares the model's expected behaviour at the moment a bet was placed
(``p_model``, ``ev_at_bet``) with the actual outcome stored after settlement.

Outputs a structured dict that the dashboard renders as:
- KPI tiles (Brier real, expected ROI vs realised ROI, hit-rate gap...).
- A calibration curve (predicted prob vs observed win rate by p_model decile).
- ROI by EV bucket vs the 2025 backtest baseline.
- Tour / surface breakdown.
- A small drift score that turns red when the model decisively underperforms.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

# --- 2025 backtest baseline (paper-trading benchmark, EV >= 8%, flat 1u) ----
# Used as the "expected" baseline against which real-life performance is
# compared. Updated whenever a new full-season backtest is run.
BACKTEST_BASELINE = {
    "global": {"hit": 0.625, "roi": 0.241, "brier": 0.183},
    "ATP": {"hit": 0.629, "roi": 0.250, "brier": 0.184},
    "WTA": {"hit": 0.613, "roi": 0.219, "brier": 0.180},
    "buckets": [
        # (lo, hi, n_year_avg, hit, roi)
        (0.08, 0.12, 234, 0.715, 0.097),
        (0.12, 0.18, 321, 0.665, 0.108),
        (0.18, 0.25, 322, 0.635, 0.090),
        (0.25, 0.30, 197, 0.633, 0.198),
    ],
}

EV_BUCKETS = [
    (0.08, 0.12),
    (0.12, 0.18),
    (0.18, 0.25),
    (0.25, 0.30),
    (0.30, 1.00),
]


def _load_settled_bets(db_path: str) -> pd.DataFrame:
    """Read every settled bet with at least p_model populated. Older legacy
    bets without p_model are excluded from drift analysis (we can't reason
    about a missing prediction)."""
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql(
            """
            SELECT id, date, match_name, bet_on, odds, stake, status, profit,
                   tour, surface, tournament, match_id,
                   p_model, p_implicit, ev_at_bet,
                   bookmaker_source, placed_ts, settled_ts,
                   winner_resolved, result_source
            FROM user_bets
            WHERE status IN ('Gagné', 'Perdu')
              AND p_model IS NOT NULL
            """,
            conn,
        )
    finally:
        conn.close()
    if df.empty:
        return df
    df["date_dt"] = pd.to_datetime(df["date"], errors="coerce")
    df["won"] = (df["status"] == "Gagné").astype(int)
    df["expected_roi_per_bet"] = df["ev_at_bet"].astype(float)
    df["realised_roi_per_bet"] = df["profit"].astype(float) / df["stake"].astype(float)
    return df


def _brier(p: pd.Series, y: pd.Series) -> float:
    if len(p) == 0:
        return float("nan")
    return float(((p - y) ** 2).mean())


def _calibration_table(df: pd.DataFrame) -> list[dict]:
    """Decile calibration: predicted prob vs observed win rate."""
    if df.empty:
        return []
    bins = np.linspace(df["p_model"].min(), 1.0, 11)
    bins = np.unique(bins)
    if len(bins) < 3:
        return []
    df = df.copy()
    df["bin"] = pd.cut(df["p_model"], bins=bins, include_lowest=True, duplicates="drop")
    grouped = df.groupby("bin", observed=True).agg(
        n=("won", "size"),
        mean_p=("p_model", "mean"),
        observed=("won", "mean"),
    ).reset_index(drop=True)
    return [
        {
            "n": int(r["n"]),
            "mean_p": float(r["mean_p"]),
            "observed": float(r["observed"]),
            "gap": float(r["observed"] - r["mean_p"]),
        }
        for _, r in grouped.iterrows()
        if r["n"] > 0
    ]


def _ev_buckets_breakdown(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    out = []
    for lo, hi in EV_BUCKETS:
        sub = df[(df["ev_at_bet"] >= lo) & (df["ev_at_bet"] < hi)]
        if sub.empty:
            continue
        n = len(sub)
        hit = float(sub["won"].mean())
        roi = float(sub["realised_roi_per_bet"].mean())
        expected_roi = float(sub["expected_roi_per_bet"].mean())
        out.append(
            {
                "ev_bucket": f"{int(lo*100)}-{int(hi*100)}%",
                "lo": lo,
                "hi": hi,
                "n": n,
                "hit": hit,
                "roi": roi,
                "expected_roi": expected_roi,
                "delta_roi": roi - expected_roi,
            }
        )
    return out


def _by_dimension(df: pd.DataFrame, col: str) -> list[dict]:
    if df.empty or col not in df.columns:
        return []
    out = []
    for key, sub in df.groupby(col):
        if sub.empty or pd.isna(key):
            continue
        n = len(sub)
        hit = float(sub["won"].mean())
        roi = float(sub["realised_roi_per_bet"].mean())
        expected_roi = float(sub["expected_roi_per_bet"].mean())
        brier = _brier(sub["p_model"], sub["won"])
        out.append(
            {
                "key": str(key),
                "n": n,
                "hit": hit,
                "roi": roi,
                "expected_roi": expected_roi,
                "delta_roi": roi - expected_roi,
                "brier": brier,
            }
        )
    return sorted(out, key=lambda r: -r["n"])


def _drift_score(global_summary: dict) -> dict:
    """Convert the gap between realised and expected into a single drift label.

    Severity rules (only relevant once n >= 30):
    - ``green``  (in line)         : delta_roi > -3pp AND brier < 0.20
    - ``amber``  (warning)         : -8pp <= delta_roi <= -3pp OR brier in [0.20, 0.22]
    - ``red``    (model drift)     : delta_roi < -8pp OR brier >= 0.22
    """
    n = global_summary.get("n", 0)
    if n < 30:
        return {
            "level": "info",
            "message": (
                f"Échantillon insuffisant ({n} paris clos) — il faut ~30 paris "
                "pour un signal fiable. Continue de paris."
            ),
        }
    delta_roi = global_summary["realised_roi"] - global_summary["expected_roi"]
    brier = global_summary["brier"]
    if delta_roi >= -0.03 and brier < 0.20:
        level = "green"
        msg = "Modèle aligné sur le backtest 2025."
    elif delta_roi >= -0.08 and brier < 0.22:
        level = "amber"
        msg = (
            "Légère sous-performance vs backtest. Surveille la prochaine quinzaine "
            "avant tout reset."
        )
    else:
        level = "red"
        msg = (
            "Drift significatif détecté. Considère un retrain (sync_tml_recent + "
            "ml_model.train) ou un audit des features récentes."
        )
    return {
        "level": level,
        "message": msg,
        "delta_roi": delta_roi,
        "brier": brier,
    }


def compute_tracking(db_path: str = "data/bettinghud.db") -> dict:
    """High-level summary of model drift on the user's settled bets."""
    df = _load_settled_bets(db_path)
    if df.empty:
        return {
            "ok": False,
            "reason": "Aucun pari enrichi clos (Gagné/Perdu) avec p_model. Place quelques paris depuis la nouvelle UI.",
        }
    n = len(df)
    hit = float(df["won"].mean())
    realised_roi = float(df["realised_roi_per_bet"].mean())
    expected_roi = float(df["expected_roi_per_bet"].mean())
    brier = _brier(df["p_model"], df["won"])
    expected_hit = float(df["p_model"].mean())

    global_summary = {
        "n": n,
        "hit": hit,
        "expected_hit": expected_hit,
        "realised_roi": realised_roi,
        "expected_roi": expected_roi,
        "brier": brier,
        "baseline_hit": BACKTEST_BASELINE["global"]["hit"],
        "baseline_roi": BACKTEST_BASELINE["global"]["roi"],
        "baseline_brier": BACKTEST_BASELINE["global"]["brier"],
    }
    return {
        "ok": True,
        "global": global_summary,
        "calibration": _calibration_table(df),
        "ev_buckets": _ev_buckets_breakdown(df),
        "by_tour": _by_dimension(df, "tour"),
        "by_surface": _by_dimension(df, "surface"),
        "drift": _drift_score(global_summary),
        "n_total_settled": n,
        "computed_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
