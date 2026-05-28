"""
Pipeline qualité données — architecture par tour :
  - ATP -> TennisMyLife (sync via scripts/sync_tml_recent.py + retraining via update_model_tml.py)
  - WTA -> Sackmann (wta_matches + rankings_wta_current)

Exécution :
  python scripts/pipeline_quality.py
  python scripts/pipeline_quality.py --with-tml-train --min-year 2010

Prérequis WTA optionnels :
  data/raw/tennis_wta/wta_matches_*.csv et wta_matches_qual_itf_*.csv
  (git clone ou scripts/fetch_wta_sackmann_raw.py)
  data/raw/tennis_wta/wta_rankings_current.csv
"""
import argparse
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(script: str) -> None:
    cmd = [sys.executable, os.path.join(ROOT, "scripts", script)]
    print("+", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)


def main():
    ap = argparse.ArgumentParser(
        description="Ingest WTA Sackmann + index SQLite ; option sync TML ATP + retraining."
    )
    ap.add_argument(
        "--with-tml-train",
        action="store_true",
        help="Après les ingests : scripts/update_model_tml.py (sync TML ATP + retraining).",
    )
    ap.add_argument(
        "--min-year",
        type=int,
        default=2010,
        help="Passé à update_model_tml si --with-tml-train.",
    )
    args = ap.parse_args()

    try:
        _run("ingest_rankings_current.py")
    except subprocess.CalledProcessError as e:
        print("ingest_rankings_current a échoué — vérifie wta_rankings_current.csv.", e)

    wta_dir = os.path.join(ROOT, "data", "raw", "tennis_wta")
    if os.path.isdir(wta_dir) and glob.glob(os.path.join(wta_dir, "wta_matches_*.csv")):
        try:
            _run("ingest_sackmann_wta.py")
        except subprocess.CalledProcessError as e:
            print("ingest_sackmann_wta a échoué.", e)
    else:
        print("(skip) Aucun wta_matches_*.csv — clone tennis_wta Sackmann si besoin.")

    try:
        _run("apply_sqlite_indexes.py")
    except subprocess.CalledProcessError as e:
        print("apply_sqlite_indexes :", e)

    if args.with_tml_train:
        cmd = [
            sys.executable,
            os.path.join(ROOT, "scripts", "update_model_tml.py"),
            "--min-year",
            str(args.min_year),
        ]
        print("+", " ".join(cmd))
        subprocess.check_call(cmd, cwd=ROOT)


if __name__ == "__main__":
    main()
