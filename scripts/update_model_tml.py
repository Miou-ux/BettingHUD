from datetime import datetime
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ml_model import TennisMLModel
from scripts.sync_tours_daily import run_sync_bundle
from scripts.bets_db import (
    DB_PATH_DEFAULT,
    META_LAST_ML_TRAIN_TS,
    ensure_bets_meta,
    set_meta,
)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def update_model(min_year=2010, skip_sync=False, output_pkl=None, feature_plot_path=None, db_path=None, segment_calib=False, tour_filter=None):
    t_pipeline = time.perf_counter()
    current_year = datetime.utcnow().year
    scope = (tour_filter or "ATP+WTA").upper()
    _log(f"=== Pipeline TML + ML | scope={scope} min_year={min_year} max_year={current_year} ===")

    t0 = time.perf_counter()
    if skip_sync:
        _log("Synchronisation réseau ignorée (--skip-sync).")
        rc_sync = 0
        dt_sync = 0.0
    else:
        _log("Début synchronisation ATP+WTA (TML + Sackmann/TA) ...")
        rc_sync = run_sync_bundle()
        if rc_sync != 0:
            _log(f"[WARN] Sync ATP+WTA incomplète (rc={rc_sync}). L'entraînement continue avec les données disponibles.")
        dt_sync = time.perf_counter() - t0
        _log(f"Fin sync ATP+WTA — durée {dt_sync:.1f}s ({dt_sync/60:.1f} min)")

    t1 = time.perf_counter()
    _log("Début chargement modèle + entraînement ...")
    ml = TennisMLModel(db_path=db_path or DB_PATH_DEFAULT)
    t_load = time.perf_counter() - t1
    _log(f"Instance TennisMLModel créée en {t_load:.2f}s")

    t2 = time.perf_counter()
    if segment_calib:
        os.environ["BETTINGHUD_SEGMENT_CALIB"] = "1"
    else:
        os.environ.pop("BETTINGHUD_SEGMENT_CALIB", None)
    train_rc = 0
    try:
        ml.train(
            min_year=min_year,
            model_path=output_pkl,
            feature_plot_path=feature_plot_path,
            tour_filter=tour_filter,
        )
    except Exception as exc:
        train_rc = 1
        _log(f"[ERROR] Entraînement ML échoué: {exc}")
    dt_train = time.perf_counter() - t2
    if train_rc == 0:
        _log(f"Fin entraînement ML — durée {dt_train:.1f}s ({dt_train/60:.1f} min)")
        _log("Clusters « Player style » : consulter le bloc stdout « --- Player style clusters --- » (tri Ace%, random_state=42).")
        _log("Matchup Synergy v4.6 : matrice style×style×surface + priors ATP/WTA (bayésien) persistés dans le bundle.")
        _log("Style Drift v4.6 : détection transition (52 semaines vs 10 derniers matchs) incluse dans les features.")
    else:
        _log(f"Pipeline interrompu après échec train ({dt_train:.1f}s).")
        return train_rc

    dt_total = time.perf_counter() - t_pipeline
    _log(f"=== TERMINÉ — total pipeline {dt_total:.1f}s ({dt_total/60:.1f} min) ===")

    try:
        conn = sqlite3.connect(DB_PATH_DEFAULT)
        try:
            ensure_bets_meta(conn)
            set_meta(
                conn,
                META_LAST_ML_TRAIN_TS,
                datetime.utcnow().isoformat(timespec="seconds"),
            )
        finally:
            conn.close()
        _log("Horodatage last_ml_train_ts enregistré dans bets_meta.")
    except Exception as exc:
        _log(f"[WARN] Impossible d'écrire last_ml_train_ts: {exc}")

    hook_default = "1" if str(os.getenv("BETTINGHUD_ENV", "")).strip().lower() == "prod" else "0"
    if os.getenv("BETTINGHUD_POST_ML_TRAIN_HOOK", hook_default).strip().lower() in ("1", "true", "yes", "on"):
        try:
            from scripts.post_ml_train_hook import run_post_ml_train_hook

            hook_rc = run_post_ml_train_hook()
            if hook_rc != 0:
                return hook_rc
        except Exception as exc:
            _log(f"[ERROR] Post-train hook: {exc}")
            try:
                from scripts.ops_telegram_alert import send_ops_alert

                send_ops_alert("Post-train hook — exception", str(exc)[:500])
            except Exception:
                pass
            return 1

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync TennisMyLife matches_recent + retrain ML model.")
    parser.add_argument("--min-year", type=int, default=2010, help="Première année (sync + filtre SQL entraînement ATP/WTA).")
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Ne pas appeler run_sync_bundle() (entraînement plus rapide sur DB locale).",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Chemin SQLite pour prepare_data (défaut : data/bettinghud.db).",
    )
    parser.add_argument(
        "--output-pkl",
        type=str,
        default=None,
        help="Chemin du bundle joblib à écrire (défaut : model_path du TennisMLModel).",
    )
    parser.add_argument(
        "--feature-plot",
        type=str,
        default=None,
        help="Chemin du PNG d'importance des features (défaut : dérivé de --output-pkl).",
    )
    parser.add_argument(
        "--segment-calib",
        action="store_true",
        help="Calibration isotonique par segment (ATP_Clay, WTA_Grass, …) en plus BO3/BO5.",
    )
    parser.add_argument(
        "--tour-filter",
        type=str,
        default=None,
        choices=("ATP", "WTA"),
        help="Entraînement isolé ATP ou WTA (split niveau 3 PREPROD).",
    )
    args = parser.parse_args()
    rc = update_model(
        min_year=args.min_year,
        skip_sync=args.skip_sync,
        output_pkl=args.output_pkl,
        feature_plot_path=args.feature_plot,
        db_path=args.db_path,
        segment_calib=args.segment_calib,
        tour_filter=args.tour_filter,
    )
    raise SystemExit(int(rc))
