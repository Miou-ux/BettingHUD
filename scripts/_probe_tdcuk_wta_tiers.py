#!/usr/bin/env python3
"""Probe tennis-data.co.uk WTA xlsx — fraîcheur par bucket Tier (P0-C).

Décide si le retard qual/ITF vient de la source ou du pipeline :
  - ITF/QUAL → fichier wta_matches_qual_itf_*
  - main tour → wta_matches_* (WTA 125/250/500/GS…)

Usage :
  python scripts/_probe_tdcuk_wta_tiers.py
  python scripts/_probe_tdcuk_wta_tiers.py --year 2026 --refresh
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.tennis_data_results import _refresh_if_stale  # noqa: E402


def _tier_bucket(tier: object) -> str:
    t = str(tier or "").upper().strip()
    if not t:
        return "unknown"
    if "ITF" in t or t.startswith("Q") or "QUAL" in t:
        return "qual_itf"
    if "125" in t:
        return "wta_125"
    if any(x in t for x in ("250", "500", "1000", "WTA Finals", "GRAND SLAM")):
        return "wta_main"
    if "WTA" in t:
        return "wta_other"
    return "other"


def _parse_date_col(df: pd.DataFrame) -> pd.Series:
    for col in ("Date", "date", "tourney_date"):
        if col in df.columns:
            return pd.to_datetime(df[col], errors="coerce")
    return pd.Series(dtype="datetime64[ns]")


def probe_wta_tiers(year: int, *, refresh: bool = False) -> dict:
    path = _refresh_if_stale("WTA", year) if refresh else os.path.join(
        "data", "raw", "tennis_data_wta", f"{year}.xlsx"
    )
    if not path or not os.path.isfile(path):
        return {"error": f"xlsx introuvable pour {year}", "path": path}

    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    sheets = pd.read_excel(path, sheet_name=None)
    frames = [f for f in sheets.values() if isinstance(f, pd.DataFrame) and len(f)]
    if not frames:
        return {"error": "xlsx vide", "path": path}
    df = pd.concat(frames, ignore_index=True)
    df["_date"] = _parse_date_col(df)
    df = df.dropna(subset=["_date"])
    if "Tier" not in df.columns:
        return {"error": "colonne Tier absente", "path": path, "columns": list(df.columns)[:20]}

    df["_bucket"] = df["Tier"].map(_tier_bucket)
    overall_max = df["_date"].max()

    by_bucket: dict[str, dict] = {}
    for bucket, grp in df.groupby("_bucket"):
        by_bucket[str(bucket)] = {
            "rows": int(len(grp)),
            "max_date": grp["_date"].max().strftime("%Y-%m-%d"),
            "days_ago": int((datetime.now() - grp["_date"].max()).days),
            "sample_tiers": sorted(grp["Tier"].astype(str).unique().tolist())[:6],
        }

    # Compare archive CSV on disk
    work = ROOT / "data" / "raw" / "tennis_wta"
    csv_max: dict[str, str | None] = {}
    for label, pattern in (
        ("main_csv", "wta_matches_{year}.csv"),
        ("qual_itf_csv", "wta_matches_qual_itf_{year}.csv"),
    ):
        p = work / pattern.format(year=year)
        if p.is_file():
            cdf = pd.read_csv(p, usecols=["tourney_date"])
            mx = int(cdf["tourney_date"].max())
            csv_max[label] = f"{mx // 10000:04d}-{(mx // 100) % 100:02d}-{mx % 100:02d}"
        else:
            csv_max[label] = None

    qual_td = by_bucket.get("qual_itf", {}).get("max_date")
    main_buckets = ("wta_main", "wta_125", "wta_other")
    main_dates = [
        by_bucket[b]["max_date"]
        for b in main_buckets
        if b in by_bucket and by_bucket[b].get("max_date")
    ]
    main_td = max(main_dates) if main_dates else None

    if "qual_itf" not in by_bucket or not qual_td:
        verdict = "source_no_itf_in_xlsx"
    elif main_td:
        qd = datetime.strptime(qual_td, "%Y-%m-%d")
        md = datetime.strptime(main_td, "%Y-%m-%d")
        lag_days = (md - qd).days
        if lag_days <= 3:
            verdict = "source_ok_qual_fresh"
        elif lag_days > 7:
            verdict = "source_lags_itf_vs_main"
        else:
            verdict = "source_partial_lag"
    else:
        verdict = "unknown"

    return {
        "year": year,
        "xlsx_path": path,
        "xlsx_mtime": mtime.isoformat(timespec="seconds"),
        "xlsx_overall_max": overall_max.strftime("%Y-%m-%d"),
        "by_bucket": by_bucket,
        "archive_csv_max": csv_max,
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Probe tennis-data WTA par Tier.")
    ap.add_argument("--year", type=int, default=datetime.now().year)
    ap.add_argument("--refresh", action="store_true", help="Re-télécharge le xlsx courant si stale.")
    args = ap.parse_args(argv)

    out = probe_wta_tiers(int(args.year), refresh=bool(args.refresh))
    if out.get("error"):
        print(f"ERREUR: {out['error']}", file=sys.stderr)
        if out.get("path"):
            print(f"  path={out['path']}")
        return 1

    print(f"=== tennis-data WTA {out['year']} ===")
    print(f"xlsx: {out['xlsx_path']} (mtime {out['xlsx_mtime']})")
    print(f"max global: {out['xlsx_overall_max']}")
    print(f"verdict: {out['verdict']}")
    print()
    print("Par bucket Tier (xlsx):")
    for bucket, info in sorted(out["by_bucket"].items()):
        print(
            f"  {bucket:12} rows={info['rows']:5} max={info['max_date']} "
            f"({info['days_ago']}j) tiers={info['sample_tiers']}"
        )
    print()
    print("Archive locale (CSV Sackmann enrichi):")
    for k, v in out["archive_csv_max"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
