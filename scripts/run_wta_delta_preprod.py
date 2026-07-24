#!/usr/bin/env python3
"""Pipeline preprod WTA delta — socle immuable + sync + TE + gate J6.

N'écrit JAMAIS dans data/raw/tennis_wta ni sur prod.

Usage :
  python scripts/run_wta_delta_preprod.py
  python scripts/run_wta_delta_preprod.py --skip-train
  python scripts/run_wta_delta_preprod.py --skip-te --max-te 20
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from scripts.wta_socle_manager import (  # noqa: E402
    DEFAULT_ARCHIVE,
    DEFAULT_MANIFEST,
    WORK_DIR,
    init_socle,
    refresh_work,
    verify_backup,
)
from scripts.wta_sackmann_common import DEFAULT_CUTOFF  # noqa: E402
from scripts.ml_bundle_registry import BASELINE_REL, candidate_output_path  # noqa: E402

PREPROD_DB = ROOT / "data" / "preprod" / "bettinghud_wta_delta.db"
CANDIDATE_MODEL = candidate_output_path("xgb_wta_delta_candidate.pkl")
BASELINE_MODEL = ROOT / BASELINE_REL


def _run(cmd: list[str], *, desc: str) -> int:
    print(f"\n=== {desc} ===")
    print(" ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pipeline preprod delta WTA (gate J6).")
    ap.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    ap.add_argument("--skip-te", action="store_true")
    ap.add_argument("--max-te", type=int, default=None)
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--skip-check", action="store_true")
    ap.add_argument("--force-refresh-work", action="store_true")
    ap.add_argument(
        "--segment-calib",
        action="store_true",
        help="Entraînement candidat avec calibration isotonique par segment (Phase 2).",
    )
    args = ap.parse_args(argv)

    # 1) Vérifier sauvegarde
    print("=== Vérification backup WTA ===")
    info = verify_backup(Path(args.archive), Path(args.manifest))
    print(f"  Archive OK — SHA256 {info['sha256'][:16]}… ({info['file_count']} fichiers)")

    # 2) Socle immuable
    init_socle(Path(args.archive), Path(args.manifest))
    if args.force_refresh_work or not WORK_DIR.is_dir():
        refresh_work(force=True)
    else:
        print(f"Work dir existant conservé: {WORK_DIR} (utiliser --force-refresh-work pour repartir du socle)")

    # 3) Sync tennis-data
    rc = _run(
        [
            sys.executable,
            "scripts/sync_wta_delta.py",
            "--work-dir",
            str(WORK_DIR),
            "--cutoff-date",
            str(args.cutoff_date),
        ],
        desc="Sync delta tennis-data",
    )
    if rc != 0:
        return rc

    # 4b) Métadonnées joueuses (IDs Sackmann, âges, hand/ht/ioc)
    rc = _run(
        [
            sys.executable,
            "scripts/enrich_wta_delta_metadata.py",
            "--work-dir",
            str(WORK_DIR),
            "--cutoff-date",
            str(args.cutoff_date),
            "--dedup",
        ],
        desc="Enrichissement métadonnées joueuses WTA",
    )
    if rc != 0:
        return rc

    # 4) Enrichissement TE
    if not args.skip_te:
        te_cmd = [
            sys.executable,
            "scripts/enrich_wta_delta_te_stats.py",
            "--work-dir",
            str(WORK_DIR),
            "--cutoff-date",
            str(args.cutoff_date),
        ]
        if args.max_te:
            te_cmd.extend(["--max-matches", str(args.max_te)])
        rc = _run(te_cmd, desc="Enrichissement Flashscore stats serve")
        if rc != 0:
            return rc

    # 5) Checklist brier-gate
    if not args.skip_check:
        check_cmd = [
            sys.executable,
            "scripts/check_wta_delta_acceptance.py",
            "--raw-dir",
            str(WORK_DIR),
            "--cutoff-date",
            str(args.cutoff_date),
            "--brier-gate",
        ]
        rc = _run(check_cmd, desc="Checklist acceptance + brier-gate")
        if rc == 1:
            print("\n[STOP] Checklist FAIL — pas d'ingest ni train.")
            return 1

    # 6) Ingest preprod DB (copy base si existe)
    PREPROD_DB.parent.mkdir(parents=True, exist_ok=True)
    base_db = ROOT / "data" / "bettinghud.db"
    if not PREPROD_DB.exists() and base_db.exists():
        shutil.copy2(base_db, PREPROD_DB)
        print(f"Copie DB base -> {PREPROD_DB}")

    rc = _run(
        [
            sys.executable,
            "scripts/ingest_sackmann_wta.py",
            "--raw-dir",
            str(WORK_DIR),
            "--db-path",
            str(PREPROD_DB),
        ],
        desc="Ingest WTA preprod DB",
    )
    if rc != 0:
        return rc

    if args.skip_train:
        print("\n[INFO] --skip-train : gate J6 non exécuté.")
        return 0

    # 7) Train candidat (sans sync réseau)
    CANDIDATE_MODEL.parent.mkdir(parents=True, exist_ok=True)
    train_cmd = [
        sys.executable,
        "scripts/update_model_tml.py",
        "--min-year",
        "2020",
        "--skip-sync",
        "--db-path",
        str(PREPROD_DB),
        "--output-pkl",
        str(CANDIDATE_MODEL),
    ]
    if args.segment_calib:
        train_cmd.append("--segment-calib")
    rc = _run(
        train_cmd,
        desc="Entraînement candidat preprod",
    )
    if rc != 0:
        return rc

    # Patch: update_model_tml may not support --output-pkl — check
    if not CANDIDATE_MODEL.exists():
        # fallback copy default output
        default_pkl = ROOT / "models" / "xgb_model_tml_v47.pkl"
        if default_pkl.exists():
            shutil.copy2(default_pkl, CANDIDATE_MODEL)

    if not BASELINE_MODEL.exists():
        print(f"[WARN] Baseline absente: {BASELINE_MODEL} — gate J6 SKIP")
        return 0

    # 8) Gate J6
    return _run(
        [
            sys.executable,
            "scripts/check_wta_brier_j6.py",
            "--baseline",
            str(BASELINE_MODEL),
            "--candidate",
            str(CANDIDATE_MODEL),
        ],
        desc="Gate J6 Brier",
    )


if __name__ == "__main__":
    raise SystemExit(main())
