#!/usr/bin/env python3
"""Affiche les métriques Brier du bundle ML actif (comparaison rapide post-retrain)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import joblib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.bets_db import get_ml_bundle_abspath  # noqa: E402

BASELINE = {
    "label": "v47 pré-retrain juil (doc archive)",
    "global_test_brier": 0.1749,
    "tour_WTA": 0.1718,
    "tour_ATP": None,
}


def _load_metrics(path: str) -> dict:
    b = joblib.load(path)
    if not isinstance(b, dict):
        return {"error": "not a dict bundle"}
    segs = b.get("segment_brier_scores") or {}
    return {
        "path": path,
        "global_test_brier": float(b.get("global_test_brier", 0)),
        "segment_brier_scores": {str(k): float(v) for k, v in segs.items()},
        "train_rows": b.get("train_row_count"),
    }


def main() -> int:
    active = get_ml_bundle_abspath()
    paths = [("active", active)]
    backup = active + ".elo_backup"
    if os.path.isfile(backup):
        paths.append(("elo_backup", backup))

    print("=== ML bundle performance ===\n")
    for label, p in paths:
        if not os.path.isfile(p):
            print(f"{label}: missing {p}")
            continue
        m = _load_metrics(p)
        print(f"[{label}] {p}")
        print(f"  global_test_brier: {m['global_test_brier']:.4f}")
        segs = m.get("segment_brier_scores") or {}
        for key in ("tour_WTA", "tour_ATP", "ATP_Hard", "WTA_Hard", "WTA_Clay"):
            if key in segs:
                print(f"  {key}: {segs[key]:.4f}")
        print()

    m = _load_metrics(active)
    g = m.get("global_test_brier", 0)
    wta = (m.get("segment_brier_scores") or {}).get("tour_WTA")
    print("--- vs baseline doc ---")
    print(f"  global: {g:.4f} vs {BASELINE['global_test_brier']:.4f}  "
          f"({'+' if g > BASELINE['global_test_brier'] else ''}{g - BASELINE['global_test_brier']:.4f})")
    if wta is not None:
        print(f"  tour_WTA: {wta:.4f} vs {BASELINE['tour_WTA']:.4f}  "
              f"({'+' if wta > BASELINE['tour_WTA'] else ''}{wta - BASELINE['tour_WTA']:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
