import sqlite3
import random
from typing import Dict, Any

import numpy as np
import pandas as pd

from scripts.ml_model import TennisMLModel
from scripts.value_detector import ValueDetector
from scripts.simulate_day import generate_bookmaker_odds


def _safe_defaults(v, d):
    return d if pd.isna(v) else v


def compute_monthly_diagnostics(
    db_path: str = "data/bettinghud.db",
    year_start: int = 2024,
    year_end: int = 2026,
    max_matches_per_month: int = 400,
    seed: int = 42,
) -> pd.DataFrame:
    y0, y1 = sorted((int(year_start), int(year_end)))
    dmin = f"{y0}0101"
    dmax = f"{y1}1231"

    conn = sqlite3.connect(db_path)
    df = pd.read_sql(
        """
        SELECT *
        FROM matches_recent
        WHERE source='tennismylife'
          AND CAST(tourney_date AS TEXT) BETWEEN ? AND ?
        ORDER BY tourney_date ASC
        """,
        conn,
        params=(dmin, dmax),
    )
    conn.close()
    if df.empty:
        return pd.DataFrame()

    df["month"] = pd.to_datetime(df["tourney_date"], format="%Y%m%d", errors="coerce").dt.to_period("M").astype(str)
    ml = TennisMLModel(db_path=db_path)
    ml._load_bundle_if_needed()
    rng = random.Random(int(seed))
    detector = ValueDetector(min_value_threshold=0.05)

    rows = []
    for month, mdf in df.groupby("month", sort=True):
        if len(mdf) > int(max_matches_per_month):
            mdf = mdf.sample(n=int(max_matches_per_month), random_state=int(seed)).sort_values("tourney_date")

        y_true = []
        y_prob = []
        bets_ret = []
        confs = []

        for _, row in mdf.iterrows():
            is_p1_winner = rng.choice([True, False])
            if is_p1_winner:
                p1_name, p2_name = row["winner_name"], row["loser_name"]
                p1_rank, p2_rank = row["winner_rank"], row["loser_rank"]
                p1_age, p2_age = row["winner_age"], row["loser_age"]
                p1_ht, p2_ht = row["winner_ht"], row["loser_ht"]
                p1_pts, p2_pts = row["winner_rank_points"], row["loser_rank_points"]
                p1_id, p2_id = row["winner_id"], row["loser_id"]
                p1_hand, p2_hand = row["winner_hand"], row["loser_hand"]
                p1_ioc, p2_ioc = row["winner_ioc"], row["loser_ioc"]
            else:
                p1_name, p2_name = row["loser_name"], row["winner_name"]
                p1_rank, p2_rank = row["loser_rank"], row["winner_rank"]
                p1_age, p2_age = row["loser_age"], row["winner_age"]
                p1_ht, p2_ht = row["loser_ht"], row["winner_ht"]
                p1_pts, p2_pts = row["loser_rank_points"], row["winner_rank_points"]
                p1_id, p2_id = row["loser_id"], row["winner_id"]
                p1_hand, p2_hand = row["loser_hand"], row["winner_hand"]
                p1_ioc, p2_ioc = row["loser_ioc"], row["winner_ioc"]

            pred = ml.predict_match(
                surface=_safe_defaults(row["surface"], "Hard"),
                p1_name=p1_name,
                p2_name=p2_name,
                p1_rank=_safe_defaults(p1_rank, 100),
                p2_rank=_safe_defaults(p2_rank, 100),
                p1_age=_safe_defaults(p1_age, 25),
                p2_age=_safe_defaults(p2_age, 25),
                p1_ht=_safe_defaults(p1_ht, 185),
                p2_ht=_safe_defaults(p2_ht, 185),
                p1_pts=_safe_defaults(p1_pts, 1000),
                p2_pts=_safe_defaults(p2_pts, 1000),
                p1_id=p1_id,
                p2_id=p2_id,
                p1_hand=_safe_defaults(p1_hand, "U"),
                p2_hand=_safe_defaults(p2_hand, "U"),
                tournament_name=_safe_defaults(row["tourney_name"], ""),
                p1_ioc=p1_ioc,
                p2_ioc=p2_ioc,
            )
            p1_prob = float(pred["p1_win_prob"])
            y = 1 if is_p1_winner else 0
            y_true.append(y)
            y_prob.append(p1_prob)
            confs.append(float(pred.get("confidence", abs(p1_prob - 0.5) * 2)))

            bm1, bm2, _ = generate_bookmaker_odds(float(_safe_defaults(p1_pts, 1000)), float(_safe_defaults(p2_pts, 1000)))
            p1_val = detector.detect_value(bm1, pred["p1_true_odd"], confidence=pred.get("confidence"))
            p2_val = detector.detect_value(bm2, pred["p2_true_odd"], confidence=pred.get("confidence"))
            if p1_val["is_value"] and p1_val["value_pct"] >= p2_val["value_pct"]:
                bets_ret.append((bm1 - 1.0) if is_p1_winner else -1.0)
            elif p2_val["is_value"]:
                bets_ret.append((bm2 - 1.0) if (not is_p1_winner) else -1.0)

        if not y_true:
            continue
        y_true = np.array(y_true)
        y_prob = np.array(y_prob)
        y_pred = (y_prob >= 0.5).astype(int)
        acc = float((y_pred == y_true).mean())
        brier = float(np.mean((y_prob - y_true) ** 2))
        mean_conf = float(np.mean(confs))
        n_bets = int(len(bets_ret))
        roi = float(np.mean(bets_ret) * 100.0) if n_bets > 0 else 0.0

        rows.append(
            {
                "month": month,
                "matches": int(len(y_true)),
                "accuracy_pct": round(acc * 100.0, 2),
                "brier": round(brier, 4),
                "mean_conf_pct": round(mean_conf * 100.0, 2),
                "value_bets": n_bets,
                "roi_value_pct": round(roi, 2),
            }
        )

    return pd.DataFrame(rows)


def compute_feature_drift(
    db_path: str = "data/bettinghud.db",
    recent_days: int = 30,
    baseline_days: int = 365,
) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql(
        """
        SELECT tourney_date, surface, winner_rank, loser_rank, winner_age, loser_age, minutes
        FROM matches_recent
        WHERE source='tennismylife'
        """,
        conn,
    )
    conn.close()
    if df.empty:
        return pd.DataFrame()
    df["tourney_date"] = pd.to_datetime(df["tourney_date"], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["tourney_date"])
    max_date = df["tourney_date"].max()
    recent_cut = max_date - pd.Timedelta(days=int(recent_days))
    base_cut = max_date - pd.Timedelta(days=int(baseline_days))
    recent = df[df["tourney_date"] >= recent_cut]
    base = df[(df["tourney_date"] >= base_cut) & (df["tourney_date"] < recent_cut)]
    if recent.empty or base.empty:
        return pd.DataFrame()

    recent_num = pd.DataFrame(
        {
            "rank_mean": pd.concat([recent["winner_rank"], recent["loser_rank"]], ignore_index=True),
            "age_mean": pd.concat([recent["winner_age"], recent["loser_age"]], ignore_index=True),
            "minutes": recent["minutes"],
        }
    )
    base_num = pd.DataFrame(
        {
            "rank_mean": pd.concat([base["winner_rank"], base["loser_rank"]], ignore_index=True),
            "age_mean": pd.concat([base["winner_age"], base["loser_age"]], ignore_index=True),
            "minutes": base["minutes"],
        }
    )

    rows = []
    for col in ["rank_mean", "age_mean", "minutes"]:
        r = pd.to_numeric(recent_num[col], errors="coerce").dropna()
        b = pd.to_numeric(base_num[col], errors="coerce").dropna()
        if len(r) == 0 or len(b) == 0:
            continue
        mu_r, mu_b = float(r.mean()), float(b.mean())
        std_b = float(b.std()) if float(b.std()) > 1e-9 else 1.0
        z = (mu_r - mu_b) / std_b
        rows.append(
            {
                "feature": col,
                "recent_mean": round(mu_r, 3),
                "baseline_mean": round(mu_b, 3),
                "z_shift": round(float(z), 3),
                "alert": "⚠️" if abs(z) >= 1.0 else "",
            }
        )
    return pd.DataFrame(rows)

