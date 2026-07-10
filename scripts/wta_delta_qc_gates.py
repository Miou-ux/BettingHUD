"""Gates QC légers WTA delta (C1 doublons, D1 rangs main) pour post-sync."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.qc_common import QcReport  # noqa: E402
from scripts.wta_sackmann_common import DEFAULT_CUTOFF  # noqa: E402


def _wta_raw_dir() -> Path:
    return Path(ROOT) / "data" / "raw" / "tennis_wta"


def _rank_rate_post_cutoff_sqlite(db_path: str, cutoff: int) -> tuple[float, int, int]:
    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM wta_matches WHERE tourney_date > ?",
            (str(cutoff),),
        ).fetchone()[0]
        with_ranks = conn.execute(
            """
            SELECT COUNT(*) FROM wta_matches
            WHERE tourney_date > ?
              AND winner_rank IS NOT NULL AND loser_rank IS NOT NULL
              AND CAST(winner_rank AS REAL) > 0 AND CAST(loser_rank AS REAL) > 0
            """,
            (str(cutoff),),
        ).fetchone()[0]
    finally:
        conn.close()
    if not total:
        return 0.0, 0, 0
    return float(with_ranks) / float(total) * 100.0, int(with_ranks), int(total)


def run_wta_delta_qc_gates(
    *,
    raw_dir: str | Path | None = None,
    db_path: str | None = None,
    cutoff_date: int | None = None,
) -> QcReport:
    report = QcReport("wta_delta_gates")
    raw = Path(raw_dir) if raw_dir else _wta_raw_dir()
    cutoff = int(cutoff_date or DEFAULT_CUTOFF)

    try:
        from scripts.check_wta_delta_acceptance import (
            check_c_integrity,
            check_d_ranks,
            detect_cutoff_date,
            load_match_frames,
        )
    except Exception as exc:
        report.add_warning("wta_gates_import", f"check_wta_delta_acceptance indisponible: {exc}")
        return report

    if not raw.is_dir():
        report.add_warning("wta_raw_missing", f"raw_dir absent: {raw}")
        return report

    try:
        df, files = load_match_frames(str(raw), min_year=int(str(cutoff)[:4]))
    except Exception as exc:
        report.add_warning("wta_load_failed", str(exc))
        return report

    if df.empty:
        report.add_warning("wta_csv_empty", "aucun CSV WTA")
        return report

    cutoff_eff = detect_cutoff_date(str(raw), str(cutoff))
    cutoff_ts = pd.Timestamp(str(cutoff_eff))
    delta = df[df["tourney_date"] >= cutoff_ts].copy()

    main_files = [p for p in files if "qual_itf" not in os.path.basename(p).lower()]
    main_dfs = [pd.read_csv(p, low_memory=False) for p in main_files]
    main_df = pd.concat(main_dfs, ignore_index=True) if main_dfs else df.iloc[0:0].copy()
    if len(main_df):
        main_df["tourney_date"] = pd.to_datetime(main_df["tourney_date"], format="%Y%m%d", errors="coerce")
        main_df = main_df.dropna(subset=["tourney_date"])
    main_delta = main_df[main_df["tourney_date"] >= cutoff_ts].copy() if len(main_df) else main_df

    for chk in check_c_integrity(df, delta, main_delta, files):
        if chk.code != "C1":
            continue
        if chk.status == "FAIL":
            report.add_blocking("wta_c1_duplicates", f"C1 doublons: {chk.detail}", **(chk.metrics or {}))
        elif chk.status == "WARN":
            report.add_warning("wta_c1_duplicates", f"C1: {chk.detail}", **(chk.metrics or {}))

    for chk in check_d_ranks(main_delta, str(raw), raw / "wta_rankings_current.csv"):
        if chk.code != "D1":
            continue
        pct = float((chk.metrics or {}).get("pct") or 0)
        if chk.status == "FAIL":
            report.add_blocking("wta_d1_ranks", f"D1 rangs main delta: {chk.detail}", pct=pct)
        elif chk.status == "WARN":
            report.add_warning("wta_d1_ranks", f"D1 rangs main delta: {chk.detail}", pct=pct)

    dbp = db_path or os.path.join(ROOT, "data", "bettinghud.db")
    if os.path.isfile(dbp):
        rate, ok_n, tot = _rank_rate_post_cutoff_sqlite(dbp, cutoff_eff)
        if tot > 0:
            if rate < 80.0:
                report.add_blocking(
                    "wta_sqlite_ranks_post_cutoff",
                    f"SQLite rangs post-cutoff {rate:.1f}% ({ok_n}/{tot})",
                    pct=rate,
                    ok=ok_n,
                    total=tot,
                )
            elif rate < 90.0:
                report.add_warning(
                    "wta_sqlite_ranks_post_cutoff",
                    f"SQLite rangs post-cutoff {rate:.1f}% ({ok_n}/{tot})",
                    pct=rate,
                    ok=ok_n,
                    total=tot,
                )

    return report


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="QC gates WTA delta (C1/D1)")
    ap.add_argument("--raw-dir", default=str(_wta_raw_dir()))
    ap.add_argument("--db-path", default=os.path.join(ROOT, "data", "bettinghud.db"))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    args = ap.parse_args()
    report = run_wta_delta_qc_gates(
        raw_dir=args.raw_dir,
        db_path=args.db_path,
        cutoff_date=args.cutoff_date,
    )
    for line in report.summary_lines():
        print(line, flush=True)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
