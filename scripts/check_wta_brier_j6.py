#!/usr/bin/env python3
"""Gate J6 — compare Brier baseline vs candidat preprod.

Usage :
  python scripts/check_wta_brier_j6.py \\
    --baseline models/xgb_model_tml_v47.pkl \\
    --candidate models/preprod/xgb_wta_delta_candidate.pkl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import joblib

ROOT = Path(__file__).resolve().parents[1]

# Seuils _wta_delta_brier_plan.md
THRESHOLDS = {
    "global_test_brier": 0.015,
    "tour_WTA": 0.020,
    "WTA_Clay": 0.025,
    "WTA_Hard": 0.025,
    "WTA_Clay_G": 0.030,
    "WTA_Hard_G": 0.030,
}


def _load_brier(path: Path) -> dict:
    obj = joblib.load(path)
    if isinstance(obj, dict):
        return {
            "global_test_brier": float(obj["global_test_brier"]) if obj.get("global_test_brier") is not None else None,
            "segment_brier_scores": dict(obj.get("segment_brier_scores") or {}),
        }
    return {
        "global_test_brier": float(getattr(obj, "global_test_brier", None)) if getattr(obj, "global_test_brier", None) is not None else None,
        "segment_brier_scores": dict(getattr(obj, "segment_brier_scores", None) or {}),
    }


def compare_j6(baseline: Path, candidate: Path) -> dict:
    b = _load_brier(baseline)
    c = _load_brier(candidate)
    checks = []
    overall_fail = False

    bg = b.get("global_test_brier")
    cg = c.get("global_test_brier")
    if bg is not None and cg is not None:
        delta = cg - bg
        st = "PASS" if abs(delta) < THRESHOLDS["global_test_brier"] else "FAIL"
        if st == "FAIL":
            overall_fail = True
        checks.append(
            {
                "code": "J6.1",
                "key": "global_test_brier",
                "baseline": bg,
                "candidate": cg,
                "delta": delta,
                "threshold": THRESHOLDS["global_test_brier"],
                "status": st,
            }
        )

    bseg, cseg = b.get("segment_brier_scores", {}), c.get("segment_brier_scores", {})
    for key, thr in THRESHOLDS.items():
        if key == "global_test_brier":
            continue
        if key not in bseg and key not in cseg:
            continue
        bv = bseg.get(key)
        cv = cseg.get(key)
        if bv is None or cv is None:
            checks.append({"code": f"J6.{key}", "key": key, "status": "SKIP", "detail": "segment absent"})
            continue
        delta = cv - bv
        st = "PASS" if abs(delta) < thr else "FAIL"
        if st == "FAIL" and key in ("tour_WTA", "global_test_brier"):
            overall_fail = True
        if st == "FAIL" and key.startswith("WTA_"):
            overall_fail = True
        checks.append(
            {
                "code": f"J6.{key}",
                "key": key,
                "baseline": bv,
                "candidate": cv,
                "delta": delta,
                "threshold": thr,
                "status": st,
            }
        )

    return {
        "baseline": str(baseline),
        "candidate": str(candidate),
        "checks": checks,
        "go_prod": not overall_fail,
        "exit_code": 1 if overall_fail else 0,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Gate J6 Brier baseline vs candidat.")
    ap.add_argument("--baseline", default=str(ROOT / "models" / "xgb_model_tml_v47.pkl"))
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report = compare_j6(Path(args.baseline), Path(args.candidate))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Gate J6 — go_prod={report['go_prod']}")
        for chk in report["checks"]:
            print(
                f"  {chk.get('code','?'):<12} {chk.get('status','?'):<5} "
                f"{chk.get('key','')} delta={chk.get('delta', 'n/a')} (seuil {chk.get('threshold', 'n/a')})"
            )
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
