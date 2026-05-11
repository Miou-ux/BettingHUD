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


def update_model(min_year=2010):
    t_pipeline = time.perf_counter()
    current_year = datetime.utcnow().year
    _log(f"=== Pipeline TML + ML | min_year={min_year} max_year={current_year} ===")

    t0 = time.perf_counter()
    _log("Début synchronisation ATP+WTA (TML + Sackmann/TA) ...")
    rc_sync = run_sync_bundle()
    if rc_sync != 0:
        _log(f"[WARN] Sync ATP+WTA incomplète (rc={rc_sync}). L'entraînement continue avec les données disponibles.")
    dt_sync = time.perf_counter() - t0
    _log(f"Fin sync ATP+WTA — durée {dt_sync:.1f}s ({dt_sync/60:.1f} min)")

    t1 = time.perf_counter()
    _log("Début chargement modèle + entraînement ...")
    ml = TennisMLModel()
    t_load = time.perf_counter() - t1
    _log(f"Instance TennisMLModel créée en {t_load:.2f}s")

    t2 = time.perf_counter()
    ml.train()
    dt_train = time.perf_counter() - t2
    _log(f"Fin entraînement ML — durée {dt_train:.1f}s ({dt_train/60:.1f} min)")
    _log("Clusters « Player style » : consulter le bloc stdout « --- Player style clusters --- » (tri Ace%, random_state=42).")
    _log("Matchup Synergy v4.6 : matrice style×style×surface + priors ATP/WTA (bayésien) persistés dans le bundle.")
    _log("Style Drift v4.6 : détection transition (52 semaines vs 10 derniers matchs) incluse dans les features.")

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync TennisMyLife matches_recent + retrain ML model.")
    parser.add_argument("--min-year", type=int, default=2010, help="Première année à synchroniser (TML).")
    args = parser.parse_args()
    update_model(min_year=args.min_year)
