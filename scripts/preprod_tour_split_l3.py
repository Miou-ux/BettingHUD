#!/usr/bin/env python3
"""PREPROD — split niveau 3 : entraîne ATP-only + WTA-only sur snapshot DB prod.

- Split temporel 80/20 **par tour** (pas de mélange ATP/WTA à l'entraînement).
- ATP : calibration dual BO3/BO5 conservée (GC, Davis, JO historiques).
- WTA : BO3 uniquement en live (pas de branche BO5 ATP).

Usage :
  py -3 scripts/preprod_tour_split_l3.py --snapshot-from data/bettinghud.db
  py -3 scripts/preprod_tour_split_l3.py --fetch-prod
  py -3 scripts/preprod_tour_split_l3.py --train-only --eval-only
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.metrics import brier_score_loss

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.ml_tour_router import enable_preprod_routing  # noqa: E402

PREPROD_DB = ROOT / "data" / "preprod" / "bettinghud_prod_snapshot.db"
ATP_BUNDLE = ROOT / "models" / "candidates" / "xgb_atp_only_l3.pkl"
WTA_BUNDLE = ROOT / "models" / "candidates" / "xgb_wta_only_l3.pkl"
REPORT_JSON = ROOT / "data" / "preprod" / "tour_split_l3_report.json"
JOINT_BASELINE = ROOT / "models" / "xgb_model_tml_v47.pkl"


def _log(msg: str) -> None:
    print(msg, flush=True)


def snapshot_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    _log(f"[snapshot] {src} -> {dst} ({dst.stat().st_size / 1e6:.1f} MB)")


def fetch_prod_db(dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    remote = "bettinghud:/opt/bettinghud/data/bettinghud.db"
    _log(f"[fetch] scp {remote} -> {dst}")
    rc = subprocess.call(["scp", remote, str(dst)])
    if rc != 0:
        raise RuntimeError(f"scp prod DB failed (rc={rc})")


def train_tour_bundles(
    *,
    db_path: Path,
    min_year: int,
    atp_out: Path,
    wta_out: Path,
    skip_atp: bool = False,
    skip_wta: bool = False,
) -> None:
    atp_out.parent.mkdir(parents=True, exist_ok=True)
    if not skip_atp:
        _log("\n=== Train ATP-only (BO3/BO5) ===")
        ml_atp = TennisMLModel(db_path=str(db_path))
        ml_atp.train(
            min_year=min_year,
            tour_filter="ATP",
            model_path=str(atp_out),
            feature_plot_path=str(atp_out.with_suffix("")) + "_importance.png",
        )
    if not skip_wta:
        _log("\n=== Train WTA-only ===")
        ml_wta = TennisMLModel(db_path=str(db_path))
        ml_wta.train(
            min_year=min_year,
            tour_filter="WTA",
            model_path=str(wta_out),
            feature_plot_path=str(wta_out.with_suffix("")) + "_importance.png",
        )


def evaluate_split_l3(
    *,
    db_path: Path,
    min_year: int,
    atp_bundle: Path,
    wta_bundle: Path,
    joint_bundle: Path | None,
) -> dict:
    from scripts.ml_model import TennisMLModel as TML

    ml_atp = TML.from_bundle(str(atp_bundle.relative_to(ROOT)), db_path=str(db_path))
    ml_wta = TML.from_bundle(str(wta_bundle.relative_to(ROOT)), db_path=str(db_path))

    atp_eval = ml_atp.eval_temporal_holdout(min_year=min_year, tour_filter="ATP")
    wta_eval = ml_wta.eval_temporal_holdout(min_year=min_year, tour_filter="WTA")

    # Combined Brier on concatenated hold-out rows (same 80/20 per tour).
    def _holdout_arrays(ml: TML, tf: str) -> tuple[np.ndarray, np.ndarray]:
        ml._load_bundle_if_needed()
        ds = ml.prepare_data(min_year=min_year, tour_filter=tf)
        X = ds[ml.features]
        y = ds["target"]
        split_idx = int(len(ds) * 0.8)
        X_test = X.iloc[split_idx:]
        y_test = y.iloc[split_idx:].to_numpy(dtype=float)
        routing = ds.loc[X_test.index, list(TML.ROUTING_COLS_BO5)]
        y_prob = np.asarray(
            ml.predict_proba_calibrated_routed(X_test, routing=routing),
            dtype=float,
        ).ravel()
        return y_test, y_prob

    y_atp, p_atp = _holdout_arrays(ml_atp, "ATP")
    y_wta, p_wta = _holdout_arrays(ml_wta, "WTA")
    y_all = np.concatenate([y_atp, y_wta])
    p_all = np.concatenate([p_atp, p_wta])
    combined_brier = float(brier_score_loss(y_all, p_all))

    out: dict = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db_path": str(db_path),
        "min_year": min_year,
        "atp_bundle": str(atp_bundle.relative_to(ROOT)).replace("\\", "/"),
        "wta_bundle": str(wta_bundle.relative_to(ROOT)).replace("\\", "/"),
        "split_l3": {
            "global_test_brier": combined_brier,
            "tour_ATP": float(atp_eval["global_test_brier"]),
            "tour_WTA": float(wta_eval["global_test_brier"]),
            "n_test_ATP": int(atp_eval["n_test"]),
            "n_test_WTA": int(wta_eval["n_test"]),
            "atp_segments": atp_eval.get("segment_brier_scores") or {},
            "wta_segments": wta_eval.get("segment_brier_scores") or {},
        },
    }

    if joint_bundle and joint_bundle.is_file():
        ml_joint = TML.from_bundle(str(joint_bundle.relative_to(ROOT)), db_path=str(db_path))
        joint_eval = ml_joint.eval_temporal_holdout(min_year=min_year, tour_filter=None)
        ds_joint_atp = ml_joint.prepare_data(min_year=min_year, tour_filter="ATP")
        Xa = ds_joint_atp[ml_joint.features]
        ya = ds_joint_atp["target"]
        sia = int(len(ds_joint_atp) * 0.8)
        Xa_t, ya_t = Xa.iloc[sia:], ya.iloc[sia:]
        ra = ds_joint_atp.loc[Xa_t.index, list(TML.ROUTING_COLS_BO5)]
        pa = np.asarray(ml_joint.predict_proba_calibrated_routed(Xa_t, routing=ra), dtype=float).ravel()

        ds_joint_wta = ml_joint.prepare_data(min_year=min_year, tour_filter="WTA")
        Xw = ds_joint_wta[ml_joint.features]
        yw = ds_joint_wta["target"]
        siw = int(len(ds_joint_wta) * 0.8)
        Xw_t, yw_t = Xw.iloc[siw:], yw.iloc[siw:]
        rw = ds_joint_wta.loc[Xw_t.index, list(TML.ROUTING_COLS_BO5)]
        pw = np.asarray(ml_joint.predict_proba_calibrated_routed(Xw_t, routing=rw), dtype=float).ravel()

        yj = np.concatenate([ya_t.to_numpy(dtype=float), yw_t.to_numpy(dtype=float)])
        pj = np.concatenate([pa, pw])
        joint_routed_brier = float(brier_score_loss(yj, pj))

        out["joint_baseline"] = {
            "bundle": str(joint_bundle.relative_to(ROOT)).replace("\\", "/"),
            "global_test_brier_joint_train": float(joint_eval["global_test_brier"]),
            "global_test_brier_same_rows": joint_routed_brier,
            "tour_ATP": float(brier_score_loss(ya_t, pa)),
            "tour_WTA": float(brier_score_loss(yw_t, pw)),
            "delta_split_vs_joint": combined_brier - joint_routed_brier,
        }

    return out


def print_report(report: dict) -> None:
    s = report["split_l3"]
    _log("\n=== Split niveau 3 — hold-out test ===")
    _log(f"  Global mixé     : {s['global_test_brier']:.4f}  (n={s['n_test_ATP']}+{s['n_test_WTA']})")
    _log(f"  tour_ATP        : {s['tour_ATP']:.4f}")
    _log(f"  tour_WTA        : {s['tour_WTA']:.4f}")
    jb = report.get("joint_baseline")
    if jb:
        _log("\n=== Joint baseline (mêmes lignes test par tour) ===")
        _log(f"  Global joint    : {jb['global_test_brier_same_rows']:.4f}")
        _log(f"  tour_ATP joint  : {jb['tour_ATP']:.4f}")
        _log(f"  tour_WTA joint  : {jb['tour_WTA']:.4f}")
        _log(f"  Δ split - joint : {jb['delta_split_vs_joint']:+.4f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PREPROD split ATP/WTA niveau 3 (données prod).")
    ap.add_argument("--snapshot-from", default=str(ROOT / "data" / "bettinghud.db"))
    ap.add_argument("--db-path", default=str(PREPROD_DB))
    ap.add_argument("--min-year", type=int, default=2020)
    ap.add_argument("--fetch-prod", action="store_true", help="scp bettinghud.db depuis prod")
    ap.add_argument("--skip-snapshot", action="store_true")
    ap.add_argument("--train-only", action="store_true")
    ap.add_argument("--eval-only", action="store_true")
    ap.add_argument("--skip-atp-train", action="store_true")
    ap.add_argument("--skip-wta-train", action="store_true")
    ap.add_argument("--joint-baseline", default=str(JOINT_BASELINE))
    ap.add_argument("--enable-routing", action="store_true", help="Écrit .ml_tour_routing_preprod.json")
    args = ap.parse_args(argv)

    os.environ.setdefault("BETTINGHUD_ENV", "preprod")

    db_path = Path(args.db_path)
    if args.fetch_prod:
        fetch_prod_db(db_path)
    elif not args.skip_snapshot and not args.eval_only:
        src = Path(args.snapshot_from)
        if not src.is_file():
            raise FileNotFoundError(f"snapshot source missing: {src}")
        snapshot_db(src, db_path)
    elif not db_path.is_file():
        raise FileNotFoundError(f"DB preprod absente: {db_path} (utiliser --fetch-prod ou --snapshot-from)")

    if not args.eval_only:
        train_tour_bundles(
            db_path=db_path,
            min_year=args.min_year,
            atp_out=ATP_BUNDLE,
            wta_out=WTA_BUNDLE,
            skip_atp=args.skip_atp_train,
            skip_wta=args.skip_wta_train,
        )

    if not args.train_only:
        if not ATP_BUNDLE.is_file() or not WTA_BUNDLE.is_file():
            raise FileNotFoundError("Bundles L3 manquants — lancer sans --eval-only")
        joint = Path(args.joint_baseline) if args.joint_baseline else None
        report = evaluate_split_l3(
            db_path=db_path,
            min_year=args.min_year,
            atp_bundle=ATP_BUNDLE,
            wta_bundle=WTA_BUNDLE,
            joint_bundle=joint if joint and joint.is_file() else None,
        )
        REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
        REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print_report(report)
        _log(f"\n[report] {REPORT_JSON}")

    if args.enable_routing:
        cfg = enable_preprod_routing(
            atp_bundle=str(ATP_BUNDLE.relative_to(ROOT)).replace("\\", "/"),
            wta_bundle=str(WTA_BUNDLE.relative_to(ROOT)).replace("\\", "/"),
        )
        cfg["routing_level"] = 3
        cfg["train_scope"] = "ATP-only + WTA-only separate"
        Path(ROOT / "models" / ".ml_tour_routing_preprod.json").write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _log("[routing] models/.ml_tour_routing_preprod.json (niveau 3)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
