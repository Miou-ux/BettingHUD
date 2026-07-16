#!/usr/bin/env python3
"""Corrige les doublons C1 WTA (alias nom sans remap ID) + re-QC.

Usage prod :
  python scripts/fix_wta_c1_duplicates.py --work-dir data/raw/tennis_wta --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.enrich_wta_delta_metadata import reorganize_work_dir_by_year
from scripts.wta_delta_qc_gates import run_wta_delta_qc_gates
from scripts.wta_name_aliases import apply_aliases_to_work_dir
from scripts.wta_sackmann_common import DEFAULT_CUTOFF


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fix C1 doublons WTA (alias→ID + dedup).")
    ap.add_argument("--work-dir", default=str(ROOT / "data" / "raw" / "tennis_wta"))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    ap.add_argument("--apply", action="store_true", help="Écrit CSV (sinon dry-run QC only).")
    ap.add_argument("--reingest", action="store_true", help="Relance ingest_sackmann_wta après fix.")
    args = ap.parse_args(argv)
    work = Path(args.work_dir)

    print("=== QC avant ===")
    before = run_wta_delta_qc_gates(raw_dir=str(work), cutoff_date=args.cutoff_date)
    for line in before.summary_lines():
        print(line)

    if not args.apply:
        print("(dry-run — passe --apply pour corriger)")
        return 0 if before.ok else 1

    alias_stats = apply_aliases_to_work_dir(work, cutoff=args.cutoff_date)
    print("aliases:", alias_stats)
    reorg = reorganize_work_dir_by_year(work)
    print("reorganize:", reorg)

    if args.reingest:
        import subprocess

        rc = subprocess.call(
            [sys.executable, str(ROOT / "scripts" / "ingest_sackmann_wta.py")],
            cwd=str(ROOT),
        )
        print("ingest_sackmann_wta rc=", rc)

    print("=== QC après ===")
    after = run_wta_delta_qc_gates(raw_dir=str(work), cutoff_date=args.cutoff_date)
    for line in after.summary_lines():
        print(line)
    return 0 if after.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
