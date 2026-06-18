#!/usr/bin/env python3
"""Print global + WTA segment Brier from model bundle(s)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import joblib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(path: Path) -> dict:
    o = joblib.load(path)
    if isinstance(o, dict):
        return {
            "global_test_brier": o.get("global_test_brier"),
            "segment_brier_scores": dict(o.get("segment_brier_scores") or {}),
        }
    return {
        "global_test_brier": getattr(o, "global_test_brier", None),
        "segment_brier_scores": dict(getattr(o, "segment_brier_scores", None) or {}),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+", help="Paths to .pkl bundles")
    args = ap.parse_args()
    for p in args.models:
        path = Path(p)
        d = _load(path)
        print(f"=== {path.name} ===")
        print(f"global_test_brier: {d['global_test_brier']:.6f}" if d["global_test_brier"] is not None else "global_test_brier: n/a")
        seg = d["segment_brier_scores"]
        for k in sorted(seg.keys()):
            if "WTA" in k or k.startswith("tour_"):
                print(f"  {k}: {seg[k]:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
