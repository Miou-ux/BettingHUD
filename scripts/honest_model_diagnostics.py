"""Diagnostics modèle sans fuite temporelle (walk-forward au début de fenêtre).

- Entraînement no-leak : uniquement les matchs ATP TML strictement avant la
  première date de l'échantillon évalué.
- Inférence avec ``match_date`` pour la décroissance Elo au jour du match.
- Cotes book : tennis-data.co.uk si dispo, sinon synthétique (signalé).
- ROI value simulé : 1u flat, seuil EV paramétrable (défaut 15 %).
"""
from __future__ import annotations

import os
import random
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from scripts.simulate_day import (
    _build_no_leak_model,
    _load_tennis_data_odds_index,
    _norm_player_name,
    generate_bookmaker_odds,
)
from scripts.value_detector import ValueDetector


def _pct_bin_labels(bins: np.ndarray) -> list[str]:
    edges = [float(x) for x in bins]
    return [
        f"{int(round(edges[i] * 100))}-{int(round(edges[i + 1] * 100))}%"
        for i in range(len(edges) - 1)
    ]


def _tourney_date_to_iso(raw) -> str | None:
    s = str(raw or "").strip()
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return None


def run_honest_model_diagnostics(
    year_start: int,
    year_end: int,
    max_matches: int = 5000,
    seed: int = 42,
    ev_threshold_frac: float = 0.15,
    db_path: str = "data/bettinghud.db",
) -> dict[str, Any] | None:
    y0, y1 = sorted((int(year_start), int(year_end)))
    date_min = f"{y0}0101"
    date_max = f"{y1}1231"

    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql(
            """
            SELECT
                tourney_date, surface, tourney_name,
                winner_name, loser_name,
                winner_rank, loser_rank,
                winner_age, loser_age,
                winner_ht, loser_ht,
                winner_rank_points, loser_rank_points,
                winner_id, loser_id,
                winner_hand, loser_hand,
                winner_ioc, loser_ioc
            FROM matches_recent
            WHERE source = 'tennismylife'
              AND CAST(tourney_date AS TEXT) BETWEEN ? AND ?
              AND tourney_level IN ('A','M','G')
            ORDER BY tourney_date ASC
            """,
            conn,
            params=(date_min, date_max),
        )
    finally:
        conn.close()

    if df.empty:
        return None

    if max_matches and len(df) > int(max_matches):
        df = (
            df.sample(n=int(max_matches), random_state=int(seed))
            .sort_values("tourney_date")
            .reset_index(drop=True)
        )

    ml_model, train_cutoff, train_rows = _build_no_leak_model(df)
    if ml_model is None:
        return {
            "ok": False,
            "reason": (
                "Impossible d'entraîner le modèle no-leak (pas assez de matchs ATP "
                f"avant la première date de l'échantillon)."
            ),
        }

    odds_idx = _load_tennis_data_odds_index(y0, y1)
    rng = random.Random(int(seed))
    detector = ValueDetector(min_value_threshold=float(ev_threshold_frac))

    preds_rows: list[dict] = []
    bets_rows: list[dict] = []
    n_real_odds = 0
    n_synthetic_odds = 0

    for _, row in df.iterrows():
        is_p1_winner = rng.choice([True, False])
        if is_p1_winner:
            p1_name = row["winner_name"]
            p2_name = row["loser_name"]
            p1_rank, p2_rank = row["winner_rank"], row["loser_rank"]
            p1_age, p2_age = row["winner_age"], row["loser_age"]
            p1_ht, p2_ht = row["winner_ht"], row["loser_ht"]
            p1_pts, p2_pts = row["winner_rank_points"], row["loser_rank_points"]
            p1_id = row["winner_id"]
            p2_id = row["loser_id"]
            p1_hand, p2_hand = row["winner_hand"], row["loser_hand"]
            p1_ioc, p2_ioc = row["winner_ioc"], row["loser_ioc"]
        else:
            p1_name = row["loser_name"]
            p2_name = row["winner_name"]
            p1_rank, p2_rank = row["loser_rank"], row["winner_rank"]
            p1_age, p2_age = row["loser_age"], row["winner_age"]
            p1_ht, p2_ht = row["loser_ht"], row["winner_ht"]
            p1_pts, p2_pts = row["loser_rank_points"], row["winner_rank_points"]
            p1_id = row["loser_id"]
            p2_id = row["winner_id"]
            p1_hand, p2_hand = row["loser_hand"], row["winner_hand"]
            p1_ioc, p2_ioc = row["loser_ioc"], row["winner_ioc"]

        p1_rank = 100 if pd.isna(p1_rank) else p1_rank
        p2_rank = 100 if pd.isna(p2_rank) else p2_rank
        p1_age = 25 if pd.isna(p1_age) else p1_age
        p2_age = 25 if pd.isna(p2_age) else p2_age
        p1_ht = 185 if pd.isna(p1_ht) else p1_ht
        p2_ht = 185 if pd.isna(p2_ht) else p2_ht
        p1_pts = 1000 if pd.isna(p1_pts) else p1_pts
        p2_pts = 1000 if pd.isna(p2_pts) else p2_pts

        match_date = _tourney_date_to_iso(row["tourney_date"])
        if not match_date:
            continue

        try:
            pred = ml_model.predict_match(
                surface=row["surface"],
                p1_name=p1_name,
                p2_name=p2_name,
                p1_rank=p1_rank,
                p2_rank=p2_rank,
                p1_age=p1_age,
                p2_age=p2_age,
                p1_ht=p1_ht,
                p2_ht=p2_ht,
                p1_pts=p1_pts,
                p2_pts=p2_pts,
                p1_id=p1_id,
                p2_id=p2_id,
                p1_hand=p1_hand,
                p2_hand=p2_hand,
                tournament_name=row["tourney_name"],
                p1_ioc=p1_ioc,
                p2_ioc=p2_ioc,
                tour="ATP",
                match_date=match_date,
            )
        except Exception:
            continue

        y_true = 1 if is_p1_winner else 0
        p1_prob = float(pred["p1_win_prob"])
        conf = float(pred.get("confidence", abs(p1_prob - 0.5) * 2))
        preds_rows.append({"p1_prob": p1_prob, "y_true": y_true, "confidence": conf})

        key = (
            match_date,
            _norm_player_name(row["winner_name"]),
            _norm_player_name(row["loser_name"]),
        )
        real_pair = odds_idx.get(key)
        if real_pair:
            odd_w, odd_l = real_pair
            if is_p1_winner:
                bm1, bm2 = odd_w, odd_l
            else:
                bm1, bm2 = odd_l, odd_w
            odds_source = "tennis_data"
            n_real_odds += 1
        else:
            bm1, bm2, _ = generate_bookmaker_odds(float(p1_pts), float(p2_pts))
            odds_source = "synthetic_rank_pts"
            n_synthetic_odds += 1

        p1_val = detector.detect_value(bm1, pred["p1_true_odd"], confidence=conf)
        p2_val = detector.detect_value(bm2, pred["p2_true_odd"], confidence=conf)

        if p1_val["is_value"] and p1_val["value_pct"] >= p2_val["value_pct"]:
            won = bool(is_p1_winner)
            bets_rows.append(
                {
                    "confidence": conf,
                    "ret": (bm1 - 1.0) if won else -1.0,
                    "odds_source": odds_source,
                }
            )
        elif p2_val["is_value"]:
            won = not bool(is_p1_winner)
            bets_rows.append(
                {
                    "confidence": conf,
                    "ret": (bm2 - 1.0) if won else -1.0,
                    "odds_source": odds_source,
                }
            )

    if not preds_rows:
        return {
            "ok": False,
            "reason": "Aucune prédiction réussie sur l'échantillon (vérifie le bundle / la base).",
        }

    dff = pd.DataFrame(preds_rows)
    _prob_bins = np.linspace(0, 1, 11)
    dff["prob_bin"] = pd.cut(
        dff["p1_prob"],
        bins=_prob_bins,
        labels=_pct_bin_labels(_prob_bins),
        include_lowest=True,
    )
    calib = (
        dff.groupby("prob_bin", observed=False)
        .agg(
            mean_prob=("p1_prob", "mean"),
            observed_win_rate=("y_true", "mean"),
            n=("y_true", "size"),
        )
        .reset_index()
    )
    calib = calib[calib["n"] > 0]

    if bets_rows:
        dfb = pd.DataFrame(bets_rows)
        _conf_bins = np.linspace(0, 1, 6)
        dfb["conf_bin"] = pd.cut(
            dfb["confidence"],
            bins=_conf_bins,
            labels=_pct_bin_labels(_conf_bins),
            include_lowest=True,
        )
        roi_conf = (
            dfb.groupby("conf_bin", observed=False)
            .agg(roi=("ret", "mean"), bets=("ret", "size"))
            .reset_index()
        )
        roi_conf = roi_conf[roi_conf["bets"] > 0]
        roi_real = dfb[dfb["odds_source"] == "tennis_data"]
        roi_syn = dfb[dfb["odds_source"] == "synthetic_rank_pts"]
        total_bets = int(len(dfb))
        total_roi = float(dfb["ret"].mean() * 100.0)
        roi_real_pct = (
            float(roi_real["ret"].mean() * 100.0) if not roi_real.empty else None
        )
        roi_syn_pct = (
            float(roi_syn["ret"].mean() * 100.0) if not roi_syn.empty else None
        )
        n_bets_real = int(len(roi_real))
        n_bets_syn = int(len(roi_syn))
    else:
        roi_conf = pd.DataFrame(columns=["conf_bin", "roi", "bets"])
        total_bets = 0
        total_roi = 0.0
        roi_real_pct = roi_syn_pct = None
        n_bets_real = n_bets_syn = 0

    n_eval = len(dff)
    pct_real = (n_real_odds / max(1, n_real_odds + n_synthetic_odds)) * 100.0

    return {
        "ok": True,
        "honest_mode": True,
        "n_matches": n_eval,
        "accuracy": float(((dff["p1_prob"] >= 0.5).astype(int) == dff["y_true"]).mean()),
        "brier": float(np.mean((dff["p1_prob"] - dff["y_true"]) ** 2)),
        "mean_confidence": float(dff["confidence"].mean()),
        "total_bets": total_bets,
        "total_roi": total_roi,
        "calibration": calib,
        "roi_by_conf": roi_conf,
        "train_cutoff": train_cutoff,
        "train_rows": int(train_rows or 0),
        "ev_threshold_pct": float(ev_threshold_frac) * 100.0,
        "n_real_odds_matches": n_real_odds,
        "n_synthetic_odds_matches": n_synthetic_odds,
        "pct_real_odds": pct_real,
        "n_bets_real_odds": n_bets_real,
        "n_bets_synthetic_odds": n_bets_syn,
        "roi_real_odds_pct": roi_real_pct,
        "roi_synthetic_odds_pct": roi_syn_pct,
        "year_start": y0,
        "year_end": y1,
    }


if __name__ == "__main__":
    import json
    import sys

    y0 = int(sys.argv[1]) if len(sys.argv) > 1 else 2023
    y1 = int(sys.argv[2]) if len(sys.argv) > 2 else 2023
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    out = run_honest_model_diagnostics(y0, y1, max_matches=n, seed=42)
    print(json.dumps(out, indent=2, default=str))
