"""Backtest 2026 sans leakage.

- Entraîne un modèle XGBoost identique à la prod sur l'historique pré-2026 uniquement
  (ATP TennisMyLife + WTA Sackmann), avec mêmes features, calibration et segments.
- Pour chaque match 2026 réellement joué, prédit la probabilité du gagnant ("p1") et,
  pour rester réaliste, randomise l'orientation P1/P2 à la décision (le modèle ignore
  qui a gagné).
- Récupère les vraies cotes Pinnacle/Bet365/B365 depuis tennis-data.co.uk
  (data/raw/tennis_data/2026.xlsx pour ATP, data/raw/tennis_data_wta/2026.xlsx pour WTA).
- Place un pari fictif de 1 unité sur le côté qui a la meilleure value, si EV ∈ [8%, 30%].
- Reporte ROI, hit rate, Brier, distribution par bucket EV, par tour et par surface.

Usage:
    python scripts/backtest_2026.py
"""

import io
import os
import re
import sys
import sqlite3
import random
import argparse
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
from xgboost import XGBClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import accuracy_score, brier_score_loss
from sklearn.model_selection import TimeSeriesSplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.surface_speed import lookup_surface_speed  # noqa: E402

# Force UTF-8 stdout on Windows
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------- name normalization (matches tennis-data odds keys) ----------

def _norm(name: str) -> str:
    n = str(name or "").lower().strip()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n)
    return n


def _surname_initial(name: str) -> str:
    """Convert 'Carlos Alcaraz' or 'Alcaraz C.' → 'alcaraz c'.

    Tennis-data and TML / Sackmann don't use the same name format. tennis-data uses
    'Alcaraz C.' style; Sackmann uses 'Carlos Alcaraz'; TML uses 'Carlos Alcaraz'.
    We match on (surname, initial) to be robust.
    """
    n = _norm(name)
    if not n:
        return ""
    parts = [p for p in n.split(" ") if p]  # drop empty tokens (e.g. trailing dot stripped)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    # Already 'last initial' format (e.g. "Tiafoe F." -> "tiafoe f")
    if len(parts[-1]) == 1:
        # surname + initial possibly with middle names. surname is parts[-2]
        return f"{parts[-2]} {parts[-1]}"
    # Standard "Firstname [Middle...] Lastname": surname = last token, initial = first letter of first token
    surname = parts[-1]
    initial = parts[0][0]
    return f"{surname} {initial}"


# ---------- bookmaker odds loader ----------

def load_bookmaker_odds_year(tour: str = "ATP", year: int = 2026):
    """Return dict {(date_iso, p1_key, p2_key): (odd_p1, odd_p2)} where p1=winner, p2=loser
    (as recorded in tennis-data files), keyed on (surname initial) for both players.
    """
    if tour == "ATP":
        path = os.path.join(ROOT, "data", "raw", "tennis_data", f"{year}.xlsx")
    else:
        path = os.path.join(ROOT, "data", "raw", "tennis_data_wta", f"{year}.xlsx")
    if not os.path.exists(path):
        print(f"  [WARN] {path} introuvable")
        return {}
    sheets = pd.read_excel(path, sheet_name=None)
    frames = [s for s in sheets.values() if isinstance(s, pd.DataFrame) and len(s) > 0]
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True, sort=False)
    cols = {c.lower(): c for c in df.columns}
    if "winner" not in cols or "loser" not in cols or "date" not in cols:
        return {}
    dser = pd.to_datetime(df[cols["date"]], errors="coerce", dayfirst=True)
    df = df.assign(_date=dser)
    df = df[df["_date"].notna()]
    df = df[df["_date"].dt.year == year]
    # AVGW/AVGL = average of all bookmakers (~97-100% coverage in 2026 file).
    # PSW (Pinnacle) is the gold standard but only ~10% filled in the 2026 archive,
    # so we prefer Avg and fall back to Pinnacle/B365/Max only if missing per-row.
    primary = ("avgw", "avgl", "AVG")
    fallbacks = [("psw", "psl", "PS"), ("b365w", "b365l", "B365"), ("maxw", "maxl", "MAX")]
    if primary[0] not in cols or primary[1] not in cols:
        # primary missing entirely → use first available fallback
        for w_key, l_key, label in fallbacks:
            if w_key in cols and l_key in cols:
                primary = (w_key, l_key, label)
                break
        else:
            return {}
    ow_col, ol_col, label = cols[primary[0]], cols[primary[1]], primary[2]
    fallback_cols = [(cols[a], cols[b], lab) for a, b, lab in fallbacks if a in cols and b in cols]
    print(f"  cotes {tour}: source primaire={label} dans {os.path.basename(path)} (fallbacks: {[f[2] for f in fallback_cols]})")
    idx = {}
    n_primary = 0
    n_fallback = 0
    n_skipped = 0
    for _, row in df.iterrows():
        try:
            dt = row["_date"].strftime("%Y-%m-%d")
            wkey = _surname_initial(row[cols["winner"]])
            lkey = _surname_initial(row[cols["loser"]])
            ow = row[ow_col] if pd.notna(row[ow_col]) else None
            ol = row[ol_col] if pd.notna(row[ol_col]) else None
            used_label = label
            if ow is None or ol is None:
                for fw, fl, flab in fallback_cols:
                    if pd.notna(row[fw]) and pd.notna(row[fl]):
                        ow, ol, used_label = row[fw], row[fl], flab
                        break
            if ow is None or ol is None:
                n_skipped += 1
                continue
            ow = float(ow)
            ol = float(ol)
            if ow <= 1.0 or ol <= 1.0:
                n_skipped += 1
                continue
            idx[(dt, wkey, lkey)] = (ow, ol, used_label)
            if used_label == label:
                n_primary += 1
            else:
                n_fallback += 1
        except Exception:
            n_skipped += 1
            continue
    print(f"  {tour}: primaire={n_primary}, fallback={n_fallback}, skipped={n_skipped} → {len(idx)} clés uniques")
    return idx


# ---------- prepare_data variant that keeps identity columns ----------

def build_dataset_with_identity(ml: TennisMLModel):
    """Same logic as ml.prepare_data() but also returns winner_name/loser_name/tour
    aligned with df1 (target=1) rows so we can join with bookmaker odds.

    Returns:
        dataset: full oriented dataset (df1 + df2 + sorted by date), exactly like prepare_data
        identity_winner: DataFrame[len(df)] with winner_name, loser_name, tour, surface,
                         tourney_date, tourney_level, source, indexed positionally and aligned
                         with the order of df1 rows in the dataset.
    """
    print("Chargement données ATP TML + WTA Sackmann (parallèle prepare_data)...")
    conn = sqlite3.connect(ml.db_path)
    df_atp = pd.read_sql(
        "SELECT * FROM matches_recent "
        "WHERE source='tennismylife' AND CAST(substr(tourney_date,1,4) AS INTEGER) >= 2010",
        conn,
    )
    print(f"  ATP TML rows: {len(df_atp)}")
    try:
        df_wta = pd.read_sql(
            "SELECT * FROM wta_matches "
            "WHERE CAST(substr(tourney_date,1,4) AS INTEGER) >= 2010",
            conn,
        )
        print(f"  WTA Sackmann rows: {len(df_wta)}")
    except Exception:
        df_wta = pd.DataFrame()
    conn.close()

    if not df_atp.empty:
        df_atp["tour"] = "ATP"
        df_atp["winner_id"] = "ATP::" + df_atp["winner_id"].astype(str)
        df_atp["loser_id"] = "ATP::" + df_atp["loser_id"].astype(str)
        df_atp["tourney_date"] = pd.to_datetime(df_atp["tourney_date"], format="%Y%m%d", errors="coerce")
    if not df_wta.empty:
        df_wta["tour"] = "WTA"
        df_wta["winner_id"] = "WTA::" + df_wta["winner_id"].astype(str)
        df_wta["loser_id"] = "WTA::" + df_wta["loser_id"].astype(str)
        df_wta["tourney_date"] = pd.to_datetime(df_wta["tourney_date"], errors="coerce")
        if "surface" in df_wta.columns:
            df_wta["surface"] = df_wta["surface"].astype(str).str.title().replace({"Nan": pd.NA})
        if "source" not in df_wta.columns:
            df_wta["source"] = "sackmann_wta"

    df = pd.concat([df_atp, df_wta], ignore_index=True, sort=False)
    df = df.dropna(
        subset=["winner_rank", "loser_rank", "winner_age", "loser_age", "surface", "tourney_date"]
    )
    df = df.sort_values("tourney_date").reset_index(drop=True)
    print(f"  Total après cleaning: {len(df)} (ATP={int((df['tour']=='ATP').sum())}, WTA={int((df['tour']=='WTA').sum())})")

    def _row_surface_speed(row):
        v = getattr(row, "surface_speed", None)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            return float(v)
        return float(lookup_surface_speed(getattr(row, "tourney_name", None), getattr(row, "surface", None)))

    df["surface_speed"] = df.apply(_row_surface_speed, axis=1)

    (
        w_elo,
        l_elo,
        w_s_elo,
        l_s_elo,
        w_serv,
        w_ret,
        l_serv,
        l_ret,
    ) = ml._build_elo_features(df)
    df["winner_elo_pre"] = w_elo
    df["loser_elo_pre"] = l_elo
    df["winner_surf_elo_pre"] = w_s_elo
    df["loser_surf_elo_pre"] = l_s_elo
    df["winner_service_elo_pre"] = w_serv
    df["winner_return_elo_pre"] = w_ret
    df["loser_service_elo_pre"] = l_serv
    df["loser_return_elo_pre"] = l_ret
    temporal = ml._build_temporal_features(df)
    for k, v in temporal.items():
        df[k] = v

    surface_map = {"Hard": 0, "Clay": 1, "Grass": 2, "Carpet": 3}

    def make_oriented(swap: bool, target: int):
        out = pd.DataFrame()
        out["surface"] = df["surface"]
        out["tourney_date"] = df["tourney_date"]
        out["tournament_level_encoded"] = df["tourney_level"].fillna("A").map(ml._encode_tourney_level)
        out["tour_encoded"] = (df["tour"] == "WTA").astype(float)
        if not swap:
            out["rank_diff"] = df["winner_rank"] - df["loser_rank"]
            out["age_diff"] = df["winner_age"] - df["loser_age"]
            out["ht_diff"] = df["winner_ht"] - df["loser_ht"]
            out["points_diff"] = df["winner_rank_points"] - df["loser_rank_points"]
            out["service_elo_diff"] = df["winner_service_elo_pre"] - df["loser_service_elo_pre"]
            out["return_elo_diff"] = df["winner_return_elo_pre"] - df["loser_return_elo_pre"]
            out["speed_affinity"] = df["winner_speed_affinity"] - df["loser_speed_affinity"]
            out["speed_performance_delta"] = df["winner_speed_perf_delta"] - df["loser_speed_perf_delta"]
            out["serve_speed_interaction"] = out["service_elo_diff"] * df["surface_speed"].astype(float)
            out["wta_weighted_advantage"] = out["tour_encoded"] * (
                0.6 * out["return_elo_diff"] + 0.4 * out["service_elo_diff"]
            )
            out["wta_speed_power_impact"] = out["tour_encoded"] * out["serve_speed_interaction"]
            out["wta_break_point_resilience"] = out["tour_encoded"] * (
                df["winner_bp_resilience"] - df["loser_bp_resilience"]
            )
            out["wins_last7d_diff"] = df["winner_wins7"] - df["loser_wins7"]
            out["three_setters_last14d_diff"] = df["winner_three14"] - df["loser_three14"]
            out["last_round_reached_diff"] = df["winner_last_round"] - df["loser_last_round"]
            out["momentum5_diff"] = df["winner_momentum5"] - df["loser_momentum5"]
            out["form90_surface_diff"] = df["winner_form90_surface"] - df["loser_form90_surface"]
            out["second_srv_ratio3_diff"] = df["winner_ssr3"] - df["loser_ssr3"]
            out["hold_surface_diff"] = df["winner_hold_surface"] - df["loser_hold_surface"]
            out["break_surface_diff"] = df["winner_break_surface"] - df["loser_break_surface"]
            out["first_srv_win10_diff"] = df["winner_first_srv10"] - df["loser_first_srv10"]
            out["bp_conv10_diff"] = df["winner_bp_conv10"] - df["loser_bp_conv10"]
            out["dominance_ratio_diff"] = df["winner_dom_ratio"] - df["loser_dom_ratio"]
            out["elo_surface_recent_diff"] = df["winner_elo_surface_recent"] - df["loser_elo_surface_recent"]
            out["hand_diff"] = (
                df["winner_hand"].map(ml._encode_hand).fillna(0)
                - df["loser_hand"].map(ml._encode_hand).fillna(0)
            )
            out["h2h_ratio"] = df["winner_h2h_ratio"]
            out["h2h_significant"] = df["winner_h2h_sig"]
            out["style_advantage_score"] = df["winner_style_adv"]
            out["clutch_index_diff"] = df["winner_clutch_idx"] - df["loser_clutch_idx"]
            out["inactivity_decay_weight"] = df["winner_inactivity_decay"] - df["loser_inactivity_decay"]
            out["home_adv_diff"] = df["winner_home"] - df["loser_home"]
        else:
            out["rank_diff"] = df["loser_rank"] - df["winner_rank"]
            out["age_diff"] = df["loser_age"] - df["winner_age"]
            out["ht_diff"] = df["loser_ht"] - df["winner_ht"]
            out["points_diff"] = df["loser_rank_points"] - df["winner_rank_points"]
            out["service_elo_diff"] = df["loser_service_elo_pre"] - df["winner_service_elo_pre"]
            out["return_elo_diff"] = df["loser_return_elo_pre"] - df["winner_return_elo_pre"]
            out["speed_affinity"] = df["loser_speed_affinity"] - df["winner_speed_affinity"]
            out["speed_performance_delta"] = df["loser_speed_perf_delta"] - df["winner_speed_perf_delta"]
            out["serve_speed_interaction"] = out["service_elo_diff"] * df["surface_speed"].astype(float)
            out["wta_weighted_advantage"] = out["tour_encoded"] * (
                0.6 * out["return_elo_diff"] + 0.4 * out["service_elo_diff"]
            )
            out["wta_speed_power_impact"] = out["tour_encoded"] * out["serve_speed_interaction"]
            out["wta_break_point_resilience"] = out["tour_encoded"] * (
                df["loser_bp_resilience"] - df["winner_bp_resilience"]
            )
            out["wins_last7d_diff"] = df["loser_wins7"] - df["winner_wins7"]
            out["three_setters_last14d_diff"] = df["loser_three14"] - df["winner_three14"]
            out["last_round_reached_diff"] = df["loser_last_round"] - df["winner_last_round"]
            out["momentum5_diff"] = df["loser_momentum5"] - df["winner_momentum5"]
            out["form90_surface_diff"] = df["loser_form90_surface"] - df["winner_form90_surface"]
            out["second_srv_ratio3_diff"] = df["loser_ssr3"] - df["winner_ssr3"]
            out["hold_surface_diff"] = df["loser_hold_surface"] - df["winner_hold_surface"]
            out["break_surface_diff"] = df["loser_break_surface"] - df["winner_break_surface"]
            out["first_srv_win10_diff"] = df["loser_first_srv10"] - df["winner_first_srv10"]
            out["bp_conv10_diff"] = df["loser_bp_conv10"] - df["winner_bp_conv10"]
            out["dominance_ratio_diff"] = df["loser_dom_ratio"] - df["winner_dom_ratio"]
            out["elo_surface_recent_diff"] = df["loser_elo_surface_recent"] - df["winner_elo_surface_recent"]
            out["hand_diff"] = (
                df["loser_hand"].map(ml._encode_hand).fillna(0)
                - df["winner_hand"].map(ml._encode_hand).fillna(0)
            )
            out["h2h_ratio"] = df["loser_h2h_ratio"]
            out["h2h_significant"] = df["loser_h2h_sig"]
            out["style_advantage_score"] = df["loser_style_adv"]
            out["clutch_index_diff"] = df["loser_clutch_idx"] - df["winner_clutch_idx"]
            out["inactivity_decay_weight"] = df["loser_inactivity_decay"] - df["winner_inactivity_decay"]
            out["home_adv_diff"] = df["loser_home"] - df["winner_home"]
        out["altitude"] = df["altitude"]
        out["indoor"] = df["indoor"]
        out["target"] = target
        out["surface_encoded"] = df["surface"].map(surface_map).fillna(0)
        return out

    df1 = make_oriented(swap=False, target=1)
    df2 = make_oriented(swap=True, target=0)
    # Garantit les colonnes attendues par ml.features pour les deux vues orientées.
    for _feat in list(getattr(ml, "features", []) or []):
        if _feat not in df1.columns:
            df1[_feat] = 0.0
        if _feat not in df2.columns:
            df2[_feat] = 0.0
        df1[_feat] = pd.to_numeric(df1[_feat], errors="coerce").fillna(0.0)
        df2[_feat] = pd.to_numeric(df2[_feat], errors="coerce").fillna(0.0)
    dataset = pd.concat([df1, df2]).sort_values("tourney_date").reset_index(drop=True)
    # Compat v4.6+: certains signaux live n'existent pas dans l'historique backtest.
    # On force les colonnes manquantes à 0.0 pour conserver un pipeline stable.
    for feat in list(getattr(ml, "features", []) or []):
        if feat not in dataset.columns:
            dataset[feat] = 0.0
    for feat in list(getattr(ml, "features", []) or []):
        if feat in dataset.columns:
            dataset[feat] = pd.to_numeric(dataset[feat], errors="coerce").fillna(0.0)
    dataset = dataset.dropna(subset=ml.features)

    # Identity is only needed for df1 (target=1, "winner-as-p1"); we'll pair them at predict time.
    identity = pd.DataFrame({
        "winner_name": df["winner_name"].values,
        "loser_name": df["loser_name"].values,
        "tour": df["tour"].values,
        "surface": df["surface"].values,
        "tourney_name": df["tourney_name"].values if "tourney_name" in df.columns else pd.Series([""] * len(df)),
        "tourney_date": df["tourney_date"].values,
        "tourney_level": df["tourney_level"].values,
        "winner_rank_points": df["winner_rank_points"].values,
        "loser_rank_points": df["loser_rank_points"].values,
    })
    return dataset, df1, identity


# ---------- backtest core ----------

def train_no_leak(ml: TennisMLModel, dataset: pd.DataFrame, cutoff: pd.Timestamp):
    """Train a fresh model with the same hyperparameters as production, on rows < cutoff."""
    train_mask = dataset["tourney_date"] < cutoff
    test_mask = dataset["tourney_date"] >= cutoff
    print(f"\nSplit temporel: train={int(train_mask.sum())} / test={int(test_mask.sum())} (cutoff={cutoff.date()})")

    X_train = dataset.loc[train_mask, ml.features]
    y_train = dataset.loc[train_mask, "target"]

    serv_abs = X_train["service_elo_diff"].abs()
    ret_abs = X_train["return_elo_diff"].abs()
    q75_serv = max(float(serv_abs.quantile(0.75)) if not serv_abs.empty else 100.0, 50.0)
    q75_ret = max(float(ret_abs.quantile(0.75)) if not ret_abs.empty else 100.0, 50.0)
    wta_mask_train = (X_train["tour_encoded"] == 1.0).astype(float)
    wta_boost = 1.0 + 0.5 * wta_mask_train
    w_train = (
        wta_boost.values
        * (
            1.0
            + 0.65 * (serv_abs / q75_serv).clip(upper=1.0)
            + 0.65 * (ret_abs / q75_ret).clip(upper=1.0)
        ).values
    )

    print("  Entraînement XGBoost (mêmes hyperparams que prod)...")
    base_model = XGBClassifier(
        n_estimators=600, max_depth=4, min_child_weight=10,
        learning_rate=0.03, subsample=0.9, colsample_bytree=0.9,
        reg_alpha=0.5, reg_lambda=1.5,
        objective="binary:logistic", eval_metric="logloss",
        random_state=42, n_jobs=4,
    )
    base_model.fit(X_train, y_train, sample_weight=w_train)

    print("  Calibration sigmoid (TimeSeriesSplit=3) globale...")
    calibrated = CalibratedClassifierCV(base_model, method="sigmoid", cv=TimeSeriesSplit(n_splits=3))
    calibrated.fit(X_train, y_train, sample_weight=w_train)
    ml.model = calibrated

    # Segmented calibrations (same as production v3.5).
    ml.model_segments = {}
    ml.segment_train_sizes = {}
    seg_defs = [
        ("Hard_G", 0.0, 3.0, None),
        ("Hard_M", 0.0, 2.0, None),
        ("Hard_A", 0.0, 1.0, None),
        ("Clay_G", 1.0, 3.0, None),
        ("Clay_M", 1.0, 2.0, None),
        ("Clay_A", 1.0, 1.0, None),
        ("Grass_G", 2.0, 3.0, None),
        ("WTA_Clay_M", 1.0, 2.0, 1.0),
    ]
    SEG_MIN = 400
    for seg_name, surf_code, lvl_code, tour_code in seg_defs:
        seg_mask = (X_train["surface_encoded"] == surf_code) & (X_train["tournament_level_encoded"] == lvl_code)
        if tour_code is not None:
            seg_mask = seg_mask & (X_train["tour_encoded"] == tour_code)
        n_seg = int(seg_mask.sum())
        if n_seg < SEG_MIN:
            continue
        y_seg = y_train[seg_mask]
        w_seg = w_train[seg_mask.values]
        if len(np.unique(y_seg)) < 2:
            continue
        seg_model = CalibratedClassifierCV(base_model, method="sigmoid", cv=TimeSeriesSplit(n_splits=3))
        seg_model.fit(X_train[seg_mask], y_seg, sample_weight=w_seg)
        ml.model_segments[seg_name] = seg_model
        ml.segment_train_sizes[seg_name] = n_seg

    print(f"  Segments calibrés: {sorted(ml.model_segments.keys())}")
    return train_mask, test_mask


def _predict_p1_prob(ml: TennisMLModel, X_row: pd.DataFrame) -> float:
    """Predict winner-as-p1 prob using the same blend logic as production predict_match
    (segment-blended with global if applicable)."""
    surf_code = float(X_row.iloc[0]["surface_encoded"])
    lvl_code = float(X_row.iloc[0]["tournament_level_encoded"])
    surf_label = {0.0: "Hard", 1.0: "Clay", 2.0: "Grass"}.get(surf_code)
    lvl_label = {3.0: "G", 2.0: "M", 1.0: "A"}.get(lvl_code)
    seg_key = f"{surf_label}_{lvl_label}" if surf_label and lvl_label else None
    seg_model = ml.model_segments.get(seg_key)
    global_p1 = float(ml.model.predict_proba(X_row[ml.features])[0][1])
    if seg_model is not None:
        seg_p1 = float(seg_model.predict_proba(X_row[ml.features])[0][1])
        w = float(getattr(ml, "segment_blend_weight", 0.7))
        return w * seg_p1 + (1.0 - w) * global_p1
    return global_p1


def run_backtest(ml: TennisMLModel, dataset: pd.DataFrame, df1: pd.DataFrame, identity: pd.DataFrame,
                 train_mask: pd.Series, ev_min: float = 0.08, ev_max: float = 0.30,
                 random_seed: int = 42, year: int = 2026):
    """Run the backtest on 2026 matches.

    df1 is the winner-as-p1 oriented dataframe (one row per match, in same order as identity).
    We predict on df1 directly, look up real bookmaker odds and decide bets.
    """
    rng = random.Random(random_seed)

    # Filter df1+identity to target-year matches (and keep alignment)
    year_start = pd.Timestamp(f"{year}-01-01")
    year_end = pd.Timestamp(f"{year + 1}-01-01")
    test_mask_id = (identity["tourney_date"] >= year_start) & (identity["tourney_date"] < year_end)
    df1_test = df1.loc[test_mask_id].copy()
    id_test = identity.loc[test_mask_id].copy()

    # Align indices
    df1_test = df1_test.reset_index(drop=True)
    id_test = id_test.reset_index(drop=True)

    # Drop rows where required features are missing
    feat_complete = df1_test[ml.features].notna().all(axis=1)
    df1_test = df1_test.loc[feat_complete].reset_index(drop=True)
    id_test = id_test.loc[feat_complete].reset_index(drop=True)

    print(f"\nMatchs {year} prédits avec features complètes: {len(df1_test)}")
    print(f"  ATP: {int((id_test['tour']=='ATP').sum())} / WTA: {int((id_test['tour']=='WTA').sum())}")

    # Bookmaker odds
    print(f"\nChargement cotes réelles tennis-data {year}...")
    odds_atp = load_bookmaker_odds_year("ATP", year=year)
    odds_wta = load_bookmaker_odds_year("WTA", year=year)
    print(f"  cotes ATP indexées: {len(odds_atp)}")
    print(f"  cotes WTA indexées: {len(odds_wta)}")

    # Predict batch (then iterate to apply segment blend per row)
    # For speed we batch global + segments separately.
    Xfeat = df1_test[ml.features]
    print("\nPrédictions...")
    global_proba = ml.model.predict_proba(Xfeat)[:, 1]
    seg_proba_cache = {}
    for seg_key, seg_model in ml.model_segments.items():
        seg_proba_cache[seg_key] = seg_model.predict_proba(Xfeat)[:, 1]

    surf_code_arr = Xfeat["surface_encoded"].values
    lvl_code_arr = Xfeat["tournament_level_encoded"].values
    tour_arr = Xfeat["tour_encoded"].values
    surf_label_map = {0.0: "Hard", 1.0: "Clay", 2.0: "Grass"}
    lvl_label_map = {3.0: "G", 2.0: "M", 1.0: "A"}
    blend_w = float(getattr(ml, "segment_blend_weight", 0.7))
    seg_sizes = getattr(ml, "segment_train_sizes", {})

    p1_probs = np.empty(len(df1_test))
    seg_used = []
    for i in range(len(df1_test)):
        sl = surf_label_map.get(float(surf_code_arr[i]))
        ll = lvl_label_map.get(float(lvl_code_arr[i]))
        seg_key = f"{sl}_{ll}" if sl and ll else None
        # Prefer WTA-specific segment when applicable (v3.5 WTA branch).
        if float(tour_arr[i]) == 1.0 and sl and ll:
            wta_key = f"WTA_{sl}_{ll}"
            if wta_key in seg_proba_cache:
                seg_key = wta_key
        if seg_key in seg_proba_cache:
            n_seg = int(seg_sizes.get(seg_key, 1500))
            volume_factor = max(0.30, min(1.0, n_seg / 1500.0))
            bw = blend_w * volume_factor
            p1_probs[i] = bw * seg_proba_cache[seg_key][i] + (1.0 - bw) * global_proba[i]
            seg_used.append(seg_key)
        else:
            p1_probs[i] = global_proba[i]
            seg_used.append("Global")

    id_test["p1_prob"] = p1_probs
    id_test["seg_used"] = seg_used

    # Lookup odds for each match
    bets = []
    n_no_odds = 0
    for i, row in id_test.iterrows():
        wkey = _surname_initial(row["winner_name"])
        lkey = _surname_initial(row["loser_name"])
        tourney_start_iso = pd.Timestamp(row["tourney_date"]).strftime("%Y-%m-%d")
        odds_book = odds_atp if row["tour"] == "ATP" else odds_wta

        # Sackmann/TML use tourney_date = tournament START date for ALL matches in
        # that tournament (so a R32 played day 1 and a final played day 14 share
        # the same tourney_date). Tennis-data uses ACTUAL match date. We therefore
        # widen the window to a forward-only 14-day search (matches always happen
        # ON or AFTER the tournament start).
        triplet = None
        odds_date_iso: str | None = None
        # Forward search 0..+14 days, then a small backward window for safety
        deltas = list(range(0, 15)) + [-1, -2, -3]
        for delta in deltas:
            d = (pd.Timestamp(row["tourney_date"]) + pd.Timedelta(days=delta)).strftime("%Y-%m-%d")
            triplet = odds_book.get((d, wkey, lkey))
            if triplet:
                odds_date_iso = d
                break
            # Try with names swapped (in case bookmaker recorded match in opposite orientation)
            triplet_swap = odds_book.get((d, lkey, wkey))
            if triplet_swap:
                triplet = (triplet_swap[1], triplet_swap[0], triplet_swap[2])
                odds_date_iso = d
                break
        if triplet is None:
            n_no_odds += 1
            continue

        odd_winner, odd_loser, odds_source = triplet
        p_winner = float(row["p1_prob"])  # since p1=winner in df1
        p_loser = 1.0 - p_winner

        ev_winner = p_winner * odd_winner - 1.0
        ev_loser = p_loser * odd_loser - 1.0

        bet_side = None
        bet_won = None
        bet_odd = None
        bet_p_model = None
        bet_p_implied = None
        bet_ev = None

        # Pick the side with strictly higher EV inside [ev_min, ev_max].
        candidates = []
        if ev_min <= ev_winner <= ev_max:
            candidates.append(("WINNER", ev_winner, odd_winner, p_winner, 1.0 / odd_winner, True))
        if ev_min <= ev_loser <= ev_max:
            candidates.append(("LOSER", ev_loser, odd_loser, p_loser, 1.0 / odd_loser, False))
        if not candidates:
            continue
        # If both sides qualify (rare), keep the higher EV
        candidates.sort(key=lambda x: x[1], reverse=True)
        bet_side, bet_ev, bet_odd, bet_p_model, bet_p_implied, bet_won = candidates[0]

        ret = (bet_odd - 1.0) if bet_won else -1.0
        # `date` = jour calendaire côté tennis-data (clé de lookup), pour un groupement
        # Kelly/journalier cohérent (sinon tout un Grand Chelem partage tourney_start).
        bet_calendar_date = odds_date_iso or tourney_start_iso
        bets.append({
            "tour": row["tour"],
            "date": bet_calendar_date,
            "tournament": row.get("tourney_name", ""),
            "surface": row["surface"],
            "tourney_level": row["tourney_level"],
            "winner_name": row["winner_name"],
            "loser_name": row["loser_name"],
            "side": bet_side,
            "p_model": bet_p_model,
            "p_implied": bet_p_implied,
            "odd": bet_odd,
            "odds_source": odds_source,
            "ev": bet_ev,
            "won": bet_won,
            "ret": ret,
            "global_p1_prob": global_proba[i],
        })

    print(f"\nMatchs sans cote bookmaker (skipped): {n_no_odds}")
    print(f"Paris placés (EV ∈ [{int(ev_min*100)}%, {int(ev_max*100)}%]): {len(bets)}")
    return pd.DataFrame(bets), id_test


# ---------- reporting ----------

def report_block(label: str, bets_df: pd.DataFrame):
    if bets_df.empty:
        print(f"\n=== {label} ===")
        print("Aucun pari placé.")
        return

    n_bets = len(bets_df)
    wins = int(bets_df["won"].sum())
    hit_rate = wins / n_bets
    total_staked = float(n_bets)  # 1 unit each
    net_profit = float(bets_df["ret"].sum())
    roi = net_profit / total_staked
    avg_odd = float(bets_df["odd"].mean())
    avg_ev = float(bets_df["ev"].mean())
    avg_p_model = float(bets_df["p_model"].mean())
    avg_p_implied = float(bets_df["p_implied"].mean())

    # Brier score on predicted prob vs outcome (1 if bet won, 0 otherwise)
    brier = float(((bets_df["p_model"] - bets_df["won"].astype(float)) ** 2).mean())

    # ROI standard error and Sharpe-like ratio (per-bet returns)
    rets = bets_df["ret"].values
    std_ret = float(np.std(rets, ddof=1)) if n_bets > 1 else 0.0
    sharpe = (np.mean(rets) / std_ret) * np.sqrt(n_bets) if std_ret > 0 else 0.0

    # Bankroll evolution
    cumret = np.cumsum(rets)
    bank = 100.0 + cumret  # start at 100 units
    peak = np.maximum.accumulate(bank)
    drawdown = (bank - peak) / peak
    max_dd = float(drawdown.min()) if len(drawdown) > 0 else 0.0

    print(f"\n=== {label} ===")
    print(f"  Paris placés:         {n_bets}")
    print(f"  Hit rate:             {hit_rate*100:.2f}%  ({wins}/{n_bets})")
    print(f"  ROI:                  {roi*100:+.2f}%   (profit: {net_profit:+.2f}u sur {total_staked:.0f}u misés)")
    print(f"  Cote moy.:            {avg_odd:.2f}")
    print(f"  EV moy.:              {avg_ev*100:+.2f}%")
    print(f"  P modèle moy.:        {avg_p_model*100:.2f}%   vs P implicite moy.: {avg_p_implied*100:.2f}%")
    print(f"  Brier (paris seuls):  {brier:.4f}")
    print(f"  Sharpe (z, total):    {sharpe:+.2f}")
    print(f"  Max drawdown:         {max_dd*100:.2f}%  (bankroll start=100u)")


def report_buckets(label: str, bets_df: pd.DataFrame):
    if bets_df.empty:
        return
    print(f"\n--- {label}: répartition par bucket EV ---")
    bins = [(0.08, 0.12), (0.12, 0.18), (0.18, 0.25), (0.25, 0.30)]
    for lo, hi in bins:
        sub = bets_df[(bets_df["ev"] >= lo) & (bets_df["ev"] < hi)]
        if sub.empty:
            print(f"  [{int(lo*100):>2d}%, {int(hi*100):>2d}%): 0 paris")
            continue
        n = len(sub)
        wins = int(sub["won"].sum())
        hit = wins / n
        roi = float(sub["ret"].sum()) / n
        print(f"  [{int(lo*100):>2d}%, {int(hi*100):>2d}%): n={n:>4d}  hit={hit*100:5.1f}%  ROI={roi*100:+6.2f}%  cote_moy={sub['odd'].mean():.2f}")


def report_by_surface(label: str, bets_df: pd.DataFrame):
    if bets_df.empty:
        return
    print(f"\n--- {label}: répartition par surface ---")
    for surf in sorted(bets_df["surface"].dropna().unique()):
        sub = bets_df[bets_df["surface"] == surf]
        if sub.empty:
            continue
        n = len(sub)
        wins = int(sub["won"].sum())
        hit = wins / n
        roi = float(sub["ret"].sum()) / n
        print(f"  {surf:<7s} n={n:>4d}  hit={hit*100:5.1f}%  ROI={roi*100:+6.2f}%")


def report_by_month(label: str, bets_df: pd.DataFrame):
    if bets_df.empty:
        return
    print(f"\n--- {label}: répartition par mois ---")
    bd = bets_df.copy()
    bd["month"] = pd.to_datetime(bd["date"]).dt.strftime("%Y-%m")
    for m in sorted(bd["month"].unique()):
        sub = bd[bd["month"] == m]
        n = len(sub)
        wins = int(sub["won"].sum())
        hit = wins / n if n else 0.0
        roi = float(sub["ret"].sum()) / n if n else 0.0
        print(f"  {m}  n={n:>4d}  hit={hit*100:5.1f}%  ROI={roi*100:+6.2f}%")


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2026, help="Target year to backtest (default 2026).")
    parser.add_argument("--ev-min", type=float, default=0.08)
    parser.add_argument("--ev-max", type=float, default=1.00)
    parser.add_argument("--cutoff", type=str, default=None, help="Training cutoff (default = year-01-01).")
    parser.add_argument("--out", type=str, default=None, help="Output CSV path.")
    args = parser.parse_args()

    target_year = int(args.year)
    cutoff = pd.Timestamp(args.cutoff) if args.cutoff else pd.Timestamp(f"{target_year}-01-01")
    out_path = args.out or f"data/backtest_{target_year}_bets.csv"

    print("=" * 78)
    print(f"BACKTEST {target_year} — sans leakage")
    print(f"  cutoff entraînement: {cutoff.date()}  (modèle ne voit AUCUN match >= cutoff)")
    print(f"  filtre EV:            [{args.ev_min*100:.0f}%, {args.ev_max*100:.0f}%]")
    print("=" * 78)

    ml = TennisMLModel()
    # Mirror production tunables
    ml.elo_decay_tau_days = 365.0
    ml.surface_blend_n0 = 30.0
    ml.segment_blend_weight = 0.7

    dataset, df1, identity = build_dataset_with_identity(ml)
    train_mask, test_mask = train_no_leak(ml, dataset, cutoff)

    bets_df, id_test = run_backtest(
        ml, dataset, df1, identity, train_mask,
        ev_min=args.ev_min, ev_max=args.ev_max, year=target_year,
    )

    if not bets_df.empty:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        bets_df.to_csv(out_path, index=False)
        print(f"\nDétail sauvegardé: {out_path}")

    # Global accuracy on target year (info only — not value-filtered)
    feat_complete = id_test["p1_prob"].notna()
    if feat_complete.any():
        sub = id_test.loc[feat_complete]
        # winner is always p1 in df1, so target=1 always for these
        # accuracy = % of times model gave p1_prob > 0.5
        acc_full = float((sub["p1_prob"] > 0.5).mean())
        brier_full = float(((sub["p1_prob"] - 1.0) ** 2).mean())
        n_atp = int((sub["tour"] == "ATP").sum())
        n_wta = int((sub["tour"] == "WTA").sum())
        acc_atp = float((sub.loc[sub["tour"] == "ATP", "p1_prob"] > 0.5).mean()) if n_atp else 0.0
        acc_wta = float((sub.loc[sub["tour"] == "WTA", "p1_prob"] > 0.5).mean()) if n_wta else 0.0
        brier_atp = float(((sub.loc[sub["tour"] == "ATP", "p1_prob"] - 1.0) ** 2).mean()) if n_atp else 0.0
        brier_wta = float(((sub.loc[sub["tour"] == "WTA", "p1_prob"] - 1.0) ** 2).mean()) if n_wta else 0.0
        print(f"\n=== Performance brute du modèle sur TOUS les matchs {target_year} (référence, sans filtre EV) ===")
        print(f"  Global: accuracy={acc_full*100:.2f}%  Brier={brier_full:.4f}  (n={len(sub)})")
        print(f"  ATP   : accuracy={acc_atp*100:.2f}%  Brier={brier_atp:.4f}  (n={n_atp})")
        print(f"  WTA   : accuracy={acc_wta*100:.2f}%  Brier={brier_wta:.4f}  (n={n_wta})")

    if bets_df.empty:
        print("\n[!] Aucun pari placé. Vérifie la couverture des cotes ou élargis les seuils EV.")
        return

    # Report by tour
    bets_atp = bets_df[bets_df["tour"] == "ATP"].copy()
    bets_wta = bets_df[bets_df["tour"] == "WTA"].copy()
    ev_label = f"EV {int(args.ev_min*100)}-{int(args.ev_max*100)}%"
    report_block(f"PARIS ATP ({ev_label})", bets_atp)
    report_buckets("PARIS ATP", bets_atp)
    report_by_surface("PARIS ATP", bets_atp)
    report_by_month("PARIS ATP", bets_atp)
    report_block(f"PARIS WTA ({ev_label})", bets_wta)
    report_buckets("PARIS WTA", bets_wta)
    report_by_surface("PARIS WTA", bets_wta)
    report_by_month("PARIS WTA", bets_wta)
    report_block(f"PARIS GLOBAL (ATP+WTA, {ev_label})", bets_df)


if __name__ == "__main__":
    main()
